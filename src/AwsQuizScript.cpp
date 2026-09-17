/*
 * mod-aws-quiz
 *
 * A virtual "character" named AWS Quiz that a player can whisper.
 * Whispering it a message either grades a pending question's answer or
 * (if no question is currently pending) triggers a fresh one on demand.
 * Separately, a WorldScript timer pushes a fresh question unprompted
 * every AwsQuiz.MinIntervalMinutes-AwsQuiz.MaxIntervalMinutes while the
 * player is online, same idea as mod-llm-guide's queue-and-deliver
 * pattern (this module was built by copying that one's proven shape —
 * see its LLMGuideScript.cpp ServerScript::CanPacketReceive for the
 * original whisper-interception trick this reuses verbatim).
 *
 * All the actual question-generation/grading work (calling an LLM) is
 * done by tools/aws_quiz_bridge.py polling aws_quiz_queue — this file
 * only ever does game-world things: notice when a question is due,
 * queue the work, and deliver the bridge's response back to the player.
 */

#include "AwsQuizConfig.h"

#include "Chat.h"
#include "DatabaseEnv.h"
#include "ObjectAccessor.h"
#include "Opcodes.h"
#include "Player.h"
#include "ScriptMgr.h"
#include "World.h"
#include "WorldPacket.h"
#include "WorldSession.h"
#include "WorldSessionMgr.h"

#include <algorithm>
#include <cctype>
#include <string>
#include <vector>

// Changed from "AWS Quiz" to a single word (Matt's request 2026-09-17) —
// no space means no quotes needed client-side: /w aws hello instead of
// /w "AWS Quiz" hello.
static const std::string QUIZ_NAME = "AWS";
// Canonical form both the whisper target and any typo-tolerant variant
// get reduced to before comparing: lowercased, spaces stripped.
static const std::string QUIZ_NAME_CANONICAL = "aws";

static std::string Canonicalize(const std::string& s)
{
    std::string out;
    out.reserve(s.size());
    for (char c : s)
    {
        if (c == ' ')
            continue;
        out += static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return out;
}

static const std::string& GetQuizLink()
{
    static const std::string link =
        "|Hplayer:" + QUIZ_NAME + "|h|cFFFFD100[AWS Quiz]|h|r";
    return link;
}

// Simple word-boundary-preferring splitter for WoW's per-line chat length
// limit — deliberately simpler than mod-llm-guide's version (no clickable
// item-link markers to protect here).
static std::vector<std::string> SplitChatChunks(const std::string& text, size_t maxLength)
{
    std::vector<std::string> chunks;
    if (text.empty())
        return chunks;

    size_t start = 0;
    while (start < text.length())
    {
        size_t end = start + maxLength;
        if (end >= text.length())
        {
            chunks.push_back(text.substr(start));
            break;
        }

        size_t lastSpace = text.rfind(' ', end);
        if (lastSpace == std::string::npos || lastSpace <= start)
            lastSpace = end;

        chunks.push_back(text.substr(start, lastSpace - start));
        start = lastSpace;
        while (start < text.length() && text[start] == ' ')
            ++start;
    }
    return chunks;
}

static void SendQuizMessage(Player* player, const std::string& response)
{
    const size_t maxLen = 240;
    ChatHandler handler(player->GetSession());
    std::string text = response.empty() ? "(no response)" : response;

    if (text.length() <= maxLen)
    {
        handler.PSendSysMessage("{}: {}", GetQuizLink(), text);
        return;
    }

    int chunkNum = 1;
    for (const std::string& chunk : SplitChatChunks(text, maxLen))
    {
        handler.PSendSysMessage("{} [{}]: {}", GetQuizLink(), chunkNum, chunk);
        ++chunkNum;
    }
}

// ServerScript: intercept whispers to "AWS Quiz" — same packet-level
// trick as mod-llm-guide uses for "AzerothGuide". No real character
// needed; the whisper target is purely virtual.
class AwsQuiz_ServerScript : public ServerScript
{
public:
    AwsQuiz_ServerScript() : ServerScript("AwsQuiz_ServerScript", {SERVERHOOK_CAN_PACKET_RECEIVE}) {}

    bool CanPacketReceive(WorldSession* session, WorldPacket const& packet) override
    {
        if (!sAwsQuizConfig->IsEnabled())
            return true;

        if (packet.GetOpcode() != CMSG_MESSAGECHAT)
            return true;

        if (packet.size() < 9)
            return true;

        WorldPacket copy(packet);

        try
        {
            uint32 type;
            uint32 lang;
            copy >> type >> lang;

            if (type != CHAT_MSG_WHISPER)
                return true;

            std::string to;
            copy >> to;

            std::string msg = copy.ReadCString(lang != LANG_ADDON);

            if (Canonicalize(to) != QUIZ_NAME_CANONICAL)
                return true;

            Player* player = session->GetPlayer();
            if (!player)
                return true;

            if (session->IsBot())
                return false;

            uint32 guid = player->GetGUID().GetCounter();
            std::string name = player->GetName();
            std::string escapedName = name;
            CharacterDatabase.EscapeString(escapedName);
            std::string escapedMsg = msg;
            CharacterDatabase.EscapeString(escapedMsg);

            CharacterDatabase.Execute(
                "INSERT IGNORE INTO aws_quiz_state (character_guid) VALUES ({})", guid);

            // Don't queue a second job if one's already in flight for
            // this player (e.g. they whispered twice in a row).
            QueryResult inFlight = CharacterDatabase.Query(
                "SELECT 1 FROM aws_quiz_queue WHERE character_guid = {} "
                "AND status IN ('pending', 'processing') LIMIT 1",
                guid);
            if (!inFlight)
            {
                CharacterDatabase.Execute(
                    "INSERT INTO aws_quiz_queue "
                    "(character_guid, character_name, kind, player_answer, status, created_at) "
                    "VALUES ({}, '{}', 'message', '{}', 'pending', NOW())",
                    guid, escapedName, escapedMsg);
            }

            LOG_DEBUG("module", "AWS Quiz: {} whispered: {}", name, msg);

            // Swallow the packet — no real "AWS Quiz" character exists,
            // so letting it through would show a "Player not found" error.
            return false;
        }
        catch (const ByteBufferException&)
        {
            return true;
        }
    }
};

// WorldScript: notices when a player is due for an unprompted question,
// queues the generate job, and delivers whatever the bridge finishes.
class AwsQuiz_WorldScript : public WorldScript
{
public:
    AwsQuiz_WorldScript()
        : WorldScript("AwsQuiz_WorldScript",
              {WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_UPDATE}) {}

    void OnAfterConfigLoad(bool /*reload*/) override
    {
        sAwsQuizConfig->LoadConfig();
    }

    void OnUpdate(uint32 diff) override
    {
        if (!sAwsQuizConfig->IsEnabled())
            return;

        static uint32 timer = 0;
        timer += diff;
        if (timer < sAwsQuizConfig->GetPollIntervalMs())
            return;
        timer = 0;

        // Expire stuck rows even if the bridge is offline.
        CharacterDatabase.DirectExecute(
            "UPDATE aws_quiz_queue SET status = 'error' "
            "WHERE status IN ('pending', 'processing') "
            "AND created_at < TIMESTAMPADD(SECOND, -{}, NOW())",
            sAwsQuizConfig->GetQueueTimeoutSeconds());

        QueueDueQuestions();
        DeliverCompletedResponses();
    }

private:
    void QueueDueQuestions()
    {
        WorldSessionMgr::SessionMap const& sessions = sWorldSessionMgr->GetAllSessions();
        for (auto const& pair : sessions)
        {
            WorldSession* session = pair.second;
            if (!session || session->PlayerLoading() || session->IsBot())
                continue;

            Player* player = session->GetPlayer();
            if (!player || !player->IsInWorld())
                continue;

            uint32 guid = player->GetGUID().GetCounter();
            std::string escapedName = player->GetName();
            CharacterDatabase.EscapeString(escapedName);

            CharacterDatabase.Execute(
                "INSERT IGNORE INTO aws_quiz_state (character_guid) VALUES ({})", guid);

            QueryResult dueCheck = CharacterDatabase.Query(
                "SELECT has_pending_question, "
                "(last_asked_at IS NULL OR "
                " TIMESTAMPADD(MINUTE, next_interval_minutes, last_asked_at) <= NOW()) AS due "
                "FROM aws_quiz_state WHERE character_guid = {}",
                guid);
            if (!dueCheck)
                continue;

            Field* fields = dueCheck->Fetch();
            bool hasPending = fields[0].Get<uint8>() != 0;
            bool due = fields[1].Get<uint8>() != 0;
            if (hasPending || !due)
                continue;

            QueryResult inFlight = CharacterDatabase.Query(
                "SELECT 1 FROM aws_quiz_queue WHERE character_guid = {} "
                "AND status IN ('pending', 'processing') LIMIT 1",
                guid);
            if (inFlight)
                continue;

            CharacterDatabase.Execute(
                "INSERT INTO aws_quiz_queue "
                "(character_guid, character_name, kind, status, created_at) "
                "VALUES ({}, '{}', 'generate', 'pending', NOW())",
                guid, escapedName);
        }
    }

    void DeliverCompletedResponses()
    {
        CharacterDatabase.DirectExecute(
            "UPDATE aws_quiz_queue SET "
            "response = IF(status = 'error', "
            "'Something went wrong generating that. Whisper me again to retry.', "
            "response), status = 'delivered' "
            "WHERE status IN ('complete', 'error') LIMIT 10");

        QueryResult result = CharacterDatabase.Query(
            "SELECT id, character_guid, character_name, response "
            "FROM aws_quiz_queue WHERE status = 'delivered'");
        if (!result)
            return;

        do
        {
            Field* fields = result->Fetch();
            uint32 id = fields[0].Get<uint32>();
            uint32 characterGuid = fields[1].Get<uint32>();
            std::string characterName = fields[2].Get<std::string>();
            std::string response = fields[3].Get<std::string>();

            Player* player = ObjectAccessor::FindPlayerByName(characterName);
            if (player && player->GetGUID().GetCounter() == characterGuid)
            {
                SendQuizMessage(player, response);
                CharacterDatabase.DirectExecute("DELETE FROM aws_quiz_queue WHERE id = {}", id);
            }
            // If offline, the row stays queued and delivers next login.
        } while (result->NextRow());
    }
};

void AddAwsQuizScripts()
{
    new AwsQuiz_ServerScript();
    new AwsQuiz_WorldScript();
}
