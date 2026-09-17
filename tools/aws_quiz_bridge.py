#!/usr/bin/env python3
"""AWS Quiz bridge for mod-aws-quiz.

Polls aws_quiz_queue (populated by the C++ side — see AwsQuizScript.cpp)
for two kinds of job:

- 'generate': push a fresh AWS Solutions Architect Professional practice
  question, weighted toward whichever exam domain the player has been
  getting wrong most often (aws_quiz_history). Stores the hidden answer
  key/explanation in aws_quiz_state and a durable row in aws_quiz_history
  (never sent to the player until they answer).
- 'message': the player whispered something. Recognized commands
  (pause/resume/skip) are handled directly, no LLM call. Otherwise, if a
  question is pending, an LLM classifies whether this is actually a
  finished answer attempt or something else (a question, "give me a
  hint", "hold on") — only real attempts get graded and recorded; anything
  else just gets a response with the question left pending. If nothing is
  pending, treat it as an on-demand request for a fresh question.

Deliberately much simpler than mod-llm-guide's bridge: no tool-calling,
no routing pass, no evidence grounding — just plain LLM calls, so this
only needs stdlib + mysql-connector, no openai/anthropic SDK dependency.
"""

import argparse
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.request

import mysql.connector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("aws_quiz_bridge")

# The 4 real AWS Certified Solutions Architect - Professional (SAP-C02)
# exam domains.
DOMAINS = [
    "Organizational Complexity",
    "New Solutions Design",
    "Continuous Improvement for Existing Solutions",
    "Accelerate Workload Migration and Modernization",
]


def parse_conf_file(path: str) -> dict:
    """Minimal AzerothCore-style `Key = value` conf parser."""
    config = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("["):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
    return config


def get_config_value(config: dict, key: str, default: str = "") -> str:
    return config.get(key, default)


def get_config_int(config: dict, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def strip_json_fences(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences despite instructions."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def add_column_if_missing(cursor, table: str, column: str, definition: str):
    """Self-healing schema migration — same pattern mod-llm-guide's
    bridge uses, so an already-deployed table can pick up new columns
    without a manual ALTER or AzerothCore's own SQL-update versioning."""
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN {definition}")
        logger.info("Added column %s.%s", table, column)


def widen_column_if_narrow(cursor, table: str, column: str, modify_clause: str):
    """Fix a column that already exists but is too narrow (e.g. an early
    VARCHAR(255) that full answer-choice text can exceed) — add_column_if_missing
    only handles columns that don't exist yet, not wrong types on ones that
    already do, so an already-deployed table needs this separate MODIFY path."""
    cursor.execute(
        "SELECT DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table, column),
    )
    row = cursor.fetchone()
    if row and row[0].lower() != "text":
        cursor.execute(f"ALTER TABLE `{table}` MODIFY COLUMN {modify_clause}")
        logger.info("Widened column %s.%s to TEXT", table, column)


GENERATE_SYSTEM_PROMPT_TEMPLATE = (
    "You are an expert AWS Certified Solutions Architect - Professional "
    "(SAP-C02) exam question writer. Generate ONE realistic, exam-quality "
    "scenario-based multiple-choice question testing professional-level "
    "AWS architecture judgment, specifically in the exam domain \"{domain}\". "
    "Vary the services and scenario each time — do not default to the "
    "most obvious/generic pairing. Make it genuinely Professional-level "
    "difficulty, not an Associate-level question. Respond with ONLY a "
    "JSON object, no other text, no markdown fences, in exactly this "
    "shape: {{\"question\": \"<realistic scenario paragraph followed by "
    "the question being asked>\", \"choices\": {{\"A\": \"...\", \"B\": "
    "\"...\", \"C\": \"...\", \"D\": \"...\"}}, \"correct\": \"<single "
    "letter A-D>\", \"explanation\": \"<why the correct answer is right "
    "and briefly why each distractor is wrong, 2-4 sentences>\"}}"
)

GRADE_SYSTEM_PROMPT = (
    "You are grading a practice AWS Certified Solutions Architect - "
    "Professional exam answer. The player's message might be: (a) a "
    "genuine finished answer attempt — a bare letter (A/B/C/D), the "
    "full answer text, or a paraphrase clearly committing to a choice; "
    "or (b) something else entirely — a clarifying question, a request "
    "for a hint, \"give me a second\", banter, or partial reasoning that "
    "doesn't commit to an answer yet. Only case (a) should be scored. "
    "Respond with ONLY a JSON object, no other text, no markdown fences, "
    "in exactly this shape: {\"is_answer_attempt\": true or false, "
    "\"correct\": true or false (meaningless if is_answer_attempt is "
    "false, still include it), \"response\": \"<message to whisper "
    "back. If is_answer_attempt is false: answer their question / give "
    "a hint without revealing the answer letter / acknowledge them, and "
    "remind them the question is still open. If is_answer_attempt is "
    "true and correct: confirm it and explain the full reasoning anyway "
    "(professional-level distractors are subtly plausible, so reinforce "
    "the why even when they got it right). If is_answer_attempt is true "
    "and incorrect: clearly state the correct letter and choice, then "
    "explain why in 2-4 sentences.>\"}"
)


class AwsQuizBridge:
    def __init__(self, config: dict):
        self.db_config = {
            "host": get_config_value(config, "AwsQuiz.Database.Host", "localhost"),
            "port": get_config_int(config, "AwsQuiz.Database.Port", 3306),
            "user": get_config_value(config, "AwsQuiz.Database.User", "acore"),
            "password": get_config_value(config, "AwsQuiz.Database.Password", "acore"),
            "database": get_config_value(config, "AwsQuiz.Database.Name", "acore_characters"),
        }
        self.api_key = get_config_value(config, "AwsQuiz.OpenRouter.ApiKey", "")
        self.model = get_config_value(config, "AwsQuiz.OpenRouter.Model", "anthropic/claude-sonnet-5")
        self.poll_interval = max(1, get_config_int(config, "AwsQuiz.Bridge.PollIntervalSeconds", 2))
        self.min_interval_minutes = get_config_int(config, "AwsQuiz.MinIntervalMinutes", 20)
        self.max_interval_minutes = get_config_int(config, "AwsQuiz.MaxIntervalMinutes", 30)
        if self.min_interval_minutes > self.max_interval_minutes:
            self.min_interval_minutes = self.max_interval_minutes

        if not self.api_key:
            logger.error("AwsQuiz.OpenRouter.ApiKey is not set — bridge cannot call the model.")
            sys.exit(1)

    def get_connection(self):
        return mysql.connector.connect(**self.db_config)

    def ensure_schema(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        add_column_if_missing(
            cursor, "aws_quiz_queue", "outcome",
            "outcome ENUM('none', 'correct', 'wrong') NOT NULL DEFAULT 'none' AFTER status",
        )
        add_column_if_missing(
            cursor, "aws_quiz_state", "is_paused",
            "is_paused TINYINT(1) NOT NULL DEFAULT 0 AFTER has_pending_question",
        )
        add_column_if_missing(
            cursor, "aws_quiz_state", "current_history_id",
            "current_history_id INT UNSIGNED DEFAULT NULL AFTER current_explanation",
        )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS `aws_quiz_history` (
              `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
              `character_guid` INT UNSIGNED NOT NULL,
              `character_name` VARCHAR(12) NOT NULL,
              `domain` VARCHAR(64) DEFAULT NULL,
              `question` TEXT NOT NULL,
              `choices_json` TEXT DEFAULT NULL,
              `correct_answer` TEXT NOT NULL,
              `explanation` TEXT NOT NULL,
              `player_answer` TEXT DEFAULT NULL,
              `was_correct` TINYINT(1) DEFAULT NULL,
              `tokens_used` INT UNSIGNED DEFAULT 0,
              `actual_cost_usd` DECIMAL(10,6) DEFAULT NULL,
              `asked_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
              `answered_at` TIMESTAMP NULL DEFAULT NULL,
              PRIMARY KEY (`id`),
              KEY `idx_character` (`character_guid`),
              KEY `idx_character_domain` (`character_guid`, `domain`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # Both were originally VARCHAR (16 / 255) — too narrow for the full
        # "letter) full choice text" strings Sonnet 5 produces, discovered
        # live via a "Data too long for column" error during pipeline
        # testing. The base SQL file (fresh installs) already uses TEXT;
        # this widens an already-deployed table in place.
        widen_column_if_narrow(
            cursor, "aws_quiz_state", "current_answer_key",
            "current_answer_key TEXT DEFAULT NULL",
        )
        widen_column_if_narrow(
            cursor, "aws_quiz_history", "correct_answer",
            "correct_answer TEXT NOT NULL",
        )
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Schema ready (aws_quiz_state.is_paused/current_history_id, aws_quiz_history)")

    # -- LLM call -----------------------------------------------------

    def call_llm(self, system_prompt: str, user_prompt: str, max_tokens: int = 2500):
        """Returns (parsed_json_dict, tokens_used, cost_usd_or_none).

        Retries once on a malformed/truncated response — measured live
        (2026-09-16): with max_tokens=1200 this failed on essentially
        every call for question generation (finish_reason sometimes
        'length', sometimes a misreported 'stop' on a still-truncated
        response — an OpenRouter/proxy-layer quirk, not just a token
        budget issue). Raising the default to 2500 fixed most of it, but
        this runs unattended every 20-30 minutes, so a single automatic
        retry against the small residual failure rate is worth the
        (rare) extra cost of a second call.
        """
        last_error = None
        for attempt in range(2):
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "usage": {"include": True},
                # Root cause found live (2026-09-16): Sonnet 5 has
                # extended thinking on by default via this route — a
                # trivial "Say OK" request burned 21 of 27 completion
                # tokens on hidden reasoning (completion_tokens_details.
                # reasoning_tokens), consuming an unpredictable chunk of
                # max_tokens before any visible output and causing
                # inconsistent truncation regardless of budget size.
                # This isn't needed for question generation/grading.
                "reasoning": {"enabled": False},
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))

            message = payload["choices"][0]["message"]["content"]
            usage = payload.get("usage") or {}
            tokens = int(usage.get("total_tokens", 0) or 0)
            cost = usage.get("cost")

            try:
                parsed = json.loads(strip_json_fences(message))
                return parsed, tokens, cost
            except json.JSONDecodeError as e:
                last_error = e
                logger.warning(
                    "Malformed JSON from model (attempt %d/2, finish_reason=%s): %s",
                    attempt + 1,
                    payload["choices"][0].get("finish_reason"),
                    e,
                )

        raise last_error

    # -- Queue plumbing -------------------------------------------------

    def fetch_pending(self, cursor):
        cursor.execute(
            "SELECT id, character_guid, character_name, kind, player_answer "
            "FROM aws_quiz_queue WHERE status = 'pending' "
            "ORDER BY created_at ASC LIMIT 5"
        )
        return cursor.fetchall()

    def mark_processing(self, cursor, request_id) -> bool:
        cursor.execute(
            "UPDATE aws_quiz_queue SET status = 'processing' "
            "WHERE id = %s AND status = 'pending'",
            (request_id,),
        )
        return cursor.rowcount == 1

    def save_response(self, cursor, request_id, response, tokens=0, cost=None, outcome="none"):
        cursor.execute(
            "UPDATE aws_quiz_queue SET status = 'complete', response = %s, "
            "outcome = %s, tokens_used = %s, actual_cost_usd = %s, processed_at = NOW() "
            "WHERE id = %s",
            (response, outcome, tokens, cost, request_id),
        )

    def save_error(self, cursor, request_id, error: str):
        logger.error("Request %s failed: %s", request_id, error)
        cursor.execute(
            "UPDATE aws_quiz_queue SET status = 'error', processed_at = NOW() "
            "WHERE id = %s",
            (request_id,),
        )

    def get_state(self, cursor, guid):
        cursor.execute(
            "SELECT has_pending_question, is_paused, current_question, "
            "current_answer_key, current_explanation, current_history_id, "
            "correct_count, wrong_count FROM aws_quiz_state "
            "WHERE character_guid = %s",
            (guid,),
        )
        return cursor.fetchone()

    # -- Domain weighting -------------------------------------------------

    def pick_domain(self, cursor, guid) -> str:
        """Weight domain selection toward whatever the player has been
        getting wrong most often, with a floor so every domain still
        gets picked sometimes (no history yet = uniform random)."""
        cursor.execute(
            "SELECT domain, "
            "SUM(CASE WHEN was_correct = 0 THEN 1 ELSE 0 END) AS wrong, "
            "COUNT(*) AS total "
            "FROM aws_quiz_history WHERE character_guid = %s "
            "AND domain IS NOT NULL AND answered_at IS NOT NULL "
            "GROUP BY domain",
            (guid,),
        )
        rows = {r["domain"]: r for r in cursor.fetchall()}

        weights = []
        for domain in DOMAINS:
            row = rows.get(domain)
            # Floor of 1 so an untried or all-correct domain can still
            # come up; wrong answers add weight on top of that floor.
            wrong = int(row["wrong"]) if row and row["wrong"] is not None else 0
            weights.append(1 + wrong * 2)

        return random.choices(DOMAINS, weights=weights, k=1)[0]

    # -- Business logic ---------------------------------------------------

    def format_question(self, parsed: dict) -> str:
        choices = parsed.get("choices", {})
        lines = [parsed.get("question", "").strip()]
        for letter in ("A", "B", "C", "D"):
            if letter in choices:
                lines.append(f"{letter}) {choices[letter]}")
        lines.append("Whisper me the letter (or your reasoning) to answer. "
                      "Whisper 'skip' for a different one, or 'pause' to stop unprompted questions.")
        return "\n".join(lines)

    def generate_question(self, cursor, request_id, guid, name):
        domain = self.pick_domain(cursor, guid)
        parsed, tokens, cost = self.call_llm(
            GENERATE_SYSTEM_PROMPT_TEMPLATE.format(domain=domain),
            "Generate one new AWS SAP-C02 practice question now.",
        )
        question_text = self.format_question(parsed)
        correct_letter = str(parsed.get("correct", "")).strip().upper()
        explanation = parsed.get("explanation", "")
        choices = parsed.get("choices", {})
        correct_full = f"{correct_letter}) {choices.get(correct_letter, '')}".strip()

        cursor.execute(
            "INSERT INTO aws_quiz_history "
            "(character_guid, character_name, domain, question, choices_json, "
            "correct_answer, explanation, tokens_used, actual_cost_usd, asked_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
            (guid, name, domain, question_text, json.dumps(choices),
             correct_full, explanation, tokens, cost),
        )
        history_id = cursor.lastrowid

        interval = random.randint(self.min_interval_minutes, self.max_interval_minutes)
        cursor.execute(
            "UPDATE aws_quiz_state SET has_pending_question = 1, "
            "current_question = %s, current_answer_key = %s, "
            "current_explanation = %s, current_history_id = %s, "
            "last_asked_at = NOW(), next_interval_minutes = %s "
            "WHERE character_guid = %s",
            (question_text, correct_full, explanation, history_id, interval, guid),
        )
        self.save_response(cursor, request_id, question_text, tokens, cost)
        logger.info(
            "Generated %s question for %s (guid=%s), next in %d min",
            domain, name, guid, interval,
        )

    def clear_pending(self, cursor, guid):
        cursor.execute(
            "UPDATE aws_quiz_state SET has_pending_question = 0, "
            "current_question = NULL, current_answer_key = NULL, "
            "current_explanation = NULL, current_history_id = NULL "
            "WHERE character_guid = %s",
            (guid,),
        )

    def handle_message(self, cursor, request_id, guid, name, raw_text):
        text = (raw_text or "").strip()
        lower = text.lower()

        # Deterministic commands — no LLM call, no ambiguity.
        if lower in ("pause", "stop"):
            cursor.execute(
                "UPDATE aws_quiz_state SET is_paused = 1 WHERE character_guid = %s", (guid,))
            self.save_response(cursor, request_id,
                "Paused — no more unprompted questions until you whisper 'resume'.")
            return

        if lower in ("resume", "start", "unpause"):
            cursor.execute(
                "UPDATE aws_quiz_state SET is_paused = 0 WHERE character_guid = %s", (guid,))
            self.save_response(cursor, request_id,
                "Resumed — you'll get questions again on the usual schedule.")
            return

        state = self.get_state(cursor, guid)

        if lower in ("skip", "next") and state and state["has_pending_question"]:
            self.clear_pending(cursor, guid)
            self.save_response(cursor, request_id, "Skipped. Here's a new one:")
            self.generate_question(cursor, request_id, guid, name)
            return

        if not state or not state["has_pending_question"]:
            # No question pending — treat this whisper as an on-demand request.
            self.generate_question(cursor, request_id, guid, name)
            return

        parsed, tokens, cost = self.call_llm(
            GRADE_SYSTEM_PROMPT,
            "Question: {}\nCorrect answer: {}\nExplanation: {}\n"
            "Player's message: \"{}\"".format(
                state["current_question"],
                state["current_answer_key"],
                state["current_explanation"],
                text,
            ),
        )
        response_text = parsed.get("response", "").strip() or "..."
        is_attempt = bool(parsed.get("is_answer_attempt"))

        if not is_attempt:
            # Not a real answer — leave the question pending, no tally
            # change, no history update. Just answer/acknowledge them.
            self.save_response(cursor, request_id, response_text, tokens, cost)
            logger.info("Non-answer message from %s (guid=%s), question stays pending", name, guid)
            return

        was_correct = bool(parsed.get("correct"))
        history_id = state["current_history_id"]

        if history_id:
            cursor.execute(
                "UPDATE aws_quiz_history SET player_answer = %s, was_correct = %s, "
                "answered_at = NOW(), tokens_used = tokens_used + %s, "
                "actual_cost_usd = actual_cost_usd + %s WHERE id = %s",
                (text, was_correct, tokens, cost or 0, history_id),
            )

        count_column = "correct_count" if was_correct else "wrong_count"
        cursor.execute(
            "UPDATE aws_quiz_state SET has_pending_question = 0, "
            f"current_question = NULL, current_answer_key = NULL, "
            f"current_explanation = NULL, current_history_id = NULL, "
            f"{count_column} = {count_column} + 1 WHERE character_guid = %s",
            (guid,),
        )

        self.save_response(cursor, request_id, response_text, tokens, cost,
                            outcome="correct" if was_correct else "wrong")
        logger.info(
            "Graded answer for %s (guid=%s): %s",
            name, guid, "correct" if was_correct else "wrong",
        )

    def process_request(self, cursor, row):
        request_id, guid, name, kind, player_answer = row
        if not self.mark_processing(cursor, request_id):
            return
        try:
            if kind == "generate":
                self.generate_question(cursor, request_id, guid, name)
            else:
                self.handle_message(cursor, request_id, guid, name, player_answer or "")
        except Exception as e:
            self.save_error(cursor, request_id, str(e))

    def run(self):
        logger.info("=" * 60)
        logger.info("AWS Quiz bridge starting...")
        logger.info("Model: %s", self.model)
        logger.info("Question interval: %d-%d minutes", self.min_interval_minutes, self.max_interval_minutes)
        logger.info("=" * 60)

        self.ensure_schema()

        while True:
            try:
                conn = self.get_connection()
                cursor = conn.cursor(dictionary=True)
                rows = self.fetch_pending(cursor)
                conn.commit()
                for row in rows:
                    self.process_request(
                        cursor,
                        (row["id"], row["character_guid"], row["character_name"],
                         row["kind"], row["player_answer"]),
                    )
                    conn.commit()
                cursor.close()
                conn.close()
            except Exception as e:
                logger.error("Bridge loop error: %s", e)
            time.sleep(self.poll_interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config = parse_conf_file(args.config)
    bridge = AwsQuizBridge(config)
    bridge.run()


if __name__ == "__main__":
    main()
