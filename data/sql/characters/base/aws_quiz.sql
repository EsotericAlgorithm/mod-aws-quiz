-- --------------------------------------------------------
-- AWS Quiz work queue
-- One row per generate/message job; deleted after delivery
-- (transient, same pattern as mod-llm-guide's llm_guide_queue).
-- --------------------------------------------------------

DROP TABLE IF EXISTS `aws_quiz_queue`;

CREATE TABLE `aws_quiz_queue` (
  `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `character_guid` INT UNSIGNED NOT NULL,
  `character_name` VARCHAR(12) NOT NULL,
  `kind` ENUM('generate', 'message') NOT NULL COMMENT 'generate = unprompted new question; message = player whispered something (answer or on-demand request)',
  `player_answer` TEXT DEFAULT NULL,
  `response` TEXT DEFAULT NULL,
  `status` ENUM('pending', 'processing', 'complete', 'delivered', 'error') NOT NULL DEFAULT 'pending',
  `tokens_used` INT UNSIGNED DEFAULT 0,
  `actual_cost_usd` DECIMAL(10,6) DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `processed_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_status` (`status`),
  KEY `idx_character_pending` (`character_guid`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AWS Quiz work queue for mod-aws-quiz';

-- --------------------------------------------------------
-- AWS Quiz per-character state
-- The current pending question (if any) plus its hidden answer key/
-- explanation live here, never sent to the player until they answer.
-- --------------------------------------------------------

DROP TABLE IF EXISTS `aws_quiz_state`;

CREATE TABLE `aws_quiz_state` (
  `character_guid` INT UNSIGNED NOT NULL,
  `correct_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `wrong_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `has_pending_question` TINYINT(1) NOT NULL DEFAULT 0,
  `is_paused` TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Set via whispering "pause" — no unprompted questions while set',
  `current_question` TEXT DEFAULT NULL COMMENT 'The presentable question+choices shown to the player',
  `current_answer_key` TEXT DEFAULT NULL COMMENT 'e.g. "B) Attach Service Control Policies..." — the full choice, not just the letter; never sent until graded',
  `current_explanation` TEXT DEFAULT NULL COMMENT 'Prepared explanation, used regardless of right/wrong',
  `current_history_id` INT UNSIGNED DEFAULT NULL COMMENT 'FK to aws_quiz_history.id for the pending question, so grading can update the same row',
  `last_asked_at` TIMESTAMP NULL DEFAULT NULL,
  `next_interval_minutes` INT UNSIGNED NOT NULL DEFAULT 25,
  PRIMARY KEY (`character_guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AWS Quiz per-character progress and pending question';

-- --------------------------------------------------------
-- AWS Quiz history
-- One row per question ever generated for a character — the durable
-- record aws_quiz_state's transient "current_*" fields don't provide.
-- Covers review (past questions/answers/explanations), cost tracking
-- (actual_cost_usd persists here, unlike the queue which is wiped after
-- delivery), and weak-area-weighted generation (domain + was_correct).
-- --------------------------------------------------------

DROP TABLE IF EXISTS `aws_quiz_history`;

CREATE TABLE `aws_quiz_history` (
  `id` INT UNSIGNED NOT NULL AUTO_INCREMENT,
  `character_guid` INT UNSIGNED NOT NULL,
  `character_name` VARCHAR(12) NOT NULL,
  `domain` VARCHAR(64) DEFAULT NULL COMMENT 'One of the 4 real SAP-C02 exam domains',
  `question` TEXT NOT NULL,
  `choices_json` TEXT DEFAULT NULL,
  `correct_answer` TEXT NOT NULL,
  `explanation` TEXT NOT NULL,
  `player_answer` TEXT DEFAULT NULL COMMENT 'NULL until answered (or stays NULL forever if skipped)',
  `was_correct` TINYINT(1) DEFAULT NULL COMMENT 'NULL until answered',
  `tokens_used` INT UNSIGNED DEFAULT 0 COMMENT 'Generation + grading calls combined',
  `actual_cost_usd` DECIMAL(10,6) DEFAULT NULL COMMENT 'Generation + grading calls combined',
  `asked_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `answered_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_character` (`character_guid`),
  KEY `idx_character_domain` (`character_guid`, `domain`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AWS Quiz durable question/answer history for mod-aws-quiz';
