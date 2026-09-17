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
  `current_question` TEXT DEFAULT NULL COMMENT 'The presentable question+choices shown to the player',
  `current_answer_key` VARCHAR(16) DEFAULT NULL COMMENT 'Correct letter, e.g. B — never sent until graded',
  `current_explanation` TEXT DEFAULT NULL COMMENT 'Prepared explanation, used regardless of right/wrong',
  `last_asked_at` TIMESTAMP NULL DEFAULT NULL,
  `next_interval_minutes` INT UNSIGNED NOT NULL DEFAULT 25,
  PRIMARY KEY (`character_guid`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AWS Quiz per-character progress and pending question';
