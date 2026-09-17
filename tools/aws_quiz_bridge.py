#!/usr/bin/env python3
"""AWS Quiz bridge for mod-aws-quiz.

Polls aws_quiz_queue (populated by the C++ side — see AwsQuizScript.cpp)
for two kinds of job:

- 'generate': push a fresh AWS Solutions Architect Professional practice
  question. Stores the hidden answer key + explanation in aws_quiz_state
  (never sent to the player) and queues the presentable question+choices
  text for delivery.
- 'message': the player whispered something. If they have a pending
  question, grade their answer against the stored key and update their
  tally. If not, treat it as an on-demand request for a fresh question
  (same as 'generate').

Deliberately much simpler than mod-llm-guide's bridge: no tool-calling,
no routing pass, no evidence grounding — just two plain LLM calls
(generate, grade), so this only needs stdlib + mysql-connector, no
openai/anthropic SDK dependency.
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


GENERATE_SYSTEM_PROMPT = (
    "You are an expert AWS Certified Solutions Architect - Professional "
    "(SAP-C02) exam question writer. Generate ONE realistic, exam-quality "
    "scenario-based multiple-choice question testing professional-level "
    "AWS architecture judgment: cost optimization at scale, complex "
    "migration strategy, multi-account/AWS Organizations design, hybrid "
    "and advanced networking, disaster recovery architecture, security "
    "and compliance governance, or performance/reliability at scale. "
    "Vary the services and scenario each time — do not default to the "
    "most obvious/generic pairing. Make it genuinely Professional-level "
    "difficulty, not an Associate-level question. Respond with ONLY a "
    "JSON object, no other text, no markdown fences, in exactly this "
    "shape: {\"question\": \"<realistic scenario paragraph followed by "
    "the question being asked>\", \"choices\": {\"A\": \"...\", \"B\": "
    "\"...\", \"C\": \"...\", \"D\": \"...\"}, \"correct\": \"<single "
    "letter A-D>\", \"explanation\": \"<why the correct answer is right "
    "and briefly why each distractor is wrong, 2-4 sentences>\"}"
)

GRADE_SYSTEM_PROMPT = (
    "You are grading a practice AWS Certified Solutions Architect - "
    "Professional exam answer. The player may respond with just a "
    "letter (A/B/C/D), the full answer text, or a paraphrase — judge "
    "the substance, not the exact wording. Respond with ONLY a JSON "
    "object, no other text, no markdown fences, in exactly this shape: "
    "{\"correct\": true or false, \"response\": \"<a concise message to "
    "whisper back: if correct, a brief affirmation plus one-sentence "
    "reinforcement of why; if incorrect, clearly state the correct "
    "letter and choice, then explain why in 2-4 sentences total>\"}"
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

    # -- LLM call -----------------------------------------------------

    def call_llm(self, system_prompt: str, user_prompt: str, max_tokens: int = 1200):
        """Returns (parsed_json_dict, tokens_used, cost_usd_or_none)."""
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "usage": {"include": True},
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

        parsed = json.loads(strip_json_fences(message))
        return parsed, tokens, cost

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

    def save_response(self, cursor, request_id, response, tokens, cost):
        cursor.execute(
            "UPDATE aws_quiz_queue SET status = 'complete', response = %s, "
            "tokens_used = %s, actual_cost_usd = %s, processed_at = NOW() "
            "WHERE id = %s",
            (response, tokens, cost, request_id),
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
            "SELECT has_pending_question, current_question, "
            "current_answer_key, current_explanation, correct_count, "
            "wrong_count FROM aws_quiz_state WHERE character_guid = %s",
            (guid,),
        )
        return cursor.fetchone()

    # -- Business logic ---------------------------------------------------

    def format_question(self, parsed: dict) -> str:
        choices = parsed.get("choices", {})
        lines = [parsed.get("question", "").strip()]
        for letter in ("A", "B", "C", "D"):
            if letter in choices:
                lines.append(f"{letter}) {choices[letter]}")
        lines.append("Whisper me the letter (or your reasoning) to answer.")
        return "\n".join(lines)

    def generate_question(self, cursor, request_id, guid, name):
        parsed, tokens, cost = self.call_llm(
            GENERATE_SYSTEM_PROMPT,
            "Generate one new AWS SAP-C02 practice question now.",
        )
        question_text = self.format_question(parsed)
        correct_letter = str(parsed.get("correct", "")).strip().upper()
        explanation = parsed.get("explanation", "")
        choices = parsed.get("choices", {})
        correct_full = f"{correct_letter}) {choices.get(correct_letter, '')}".strip()

        interval = random.randint(self.min_interval_minutes, self.max_interval_minutes)
        cursor.execute(
            "UPDATE aws_quiz_state SET has_pending_question = 1, "
            "current_question = %s, current_answer_key = %s, "
            "current_explanation = %s, last_asked_at = NOW(), "
            "next_interval_minutes = %s WHERE character_guid = %s",
            (question_text, correct_full, explanation, interval, guid),
        )
        self.save_response(cursor, request_id, question_text, tokens, cost)
        logger.info(
            "Generated question for %s (guid=%s), next in %d min",
            name, guid, interval,
        )

    def handle_message(self, cursor, request_id, guid, name, answer_text):
        state = self.get_state(cursor, guid)
        if not state or not state["has_pending_question"]:
            # No question pending — treat this whisper as an on-demand request.
            self.generate_question(cursor, request_id, guid, name)
            return

        parsed, tokens, cost = self.call_llm(
            GRADE_SYSTEM_PROMPT,
            "Question: {}\nCorrect answer: {}\nExplanation: {}\n"
            "Player's answer: \"{}\"".format(
                state["current_question"],
                state["current_answer_key"],
                state["current_explanation"],
                answer_text,
            ),
        )
        was_correct = bool(parsed.get("correct"))
        response_text = parsed.get("response", "").strip() or (
            "Correct!" if was_correct else "Not quite."
        )

        if was_correct:
            cursor.execute(
                "UPDATE aws_quiz_state SET has_pending_question = 0, "
                "current_question = NULL, current_answer_key = NULL, "
                "current_explanation = NULL, correct_count = correct_count + 1 "
                "WHERE character_guid = %s",
                (guid,),
            )
        else:
            cursor.execute(
                "UPDATE aws_quiz_state SET has_pending_question = 0, "
                "current_question = NULL, current_answer_key = NULL, "
                "current_explanation = NULL, wrong_count = wrong_count + 1 "
                "WHERE character_guid = %s",
                (guid,),
            )

        self.save_response(cursor, request_id, response_text, tokens, cost)
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
