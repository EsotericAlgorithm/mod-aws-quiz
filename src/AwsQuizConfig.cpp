/*
 * mod-aws-quiz
 */

#include "AwsQuizConfig.h"
#include "Config.h"
#include "Log.h"

AwsQuizConfig* AwsQuizConfig::instance()
{
    static AwsQuizConfig instance;
    return &instance;
}

void AwsQuizConfig::LoadConfig()
{
    _enabled = sConfigMgr->GetOption<bool>("AwsQuiz.Enable", false);
    _pollIntervalMs = sConfigMgr->GetOption<uint32>("AwsQuiz.PollIntervalMs", 2000);
    _minIntervalMinutes = sConfigMgr->GetOption<uint32>("AwsQuiz.MinIntervalMinutes", 20);
    _maxIntervalMinutes = sConfigMgr->GetOption<uint32>("AwsQuiz.MaxIntervalMinutes", 30);
    _queueTimeoutSeconds = sConfigMgr->GetOption<uint32>("AwsQuiz.QueueTimeoutSeconds", 120);

    if (_minIntervalMinutes > _maxIntervalMinutes)
        _minIntervalMinutes = _maxIntervalMinutes;

    if (_enabled)
    {
        LOG_INFO("module", ">> AWS Quiz module loaded");
        LOG_INFO("module", "   Question interval: {}-{} minutes", _minIntervalMinutes, _maxIntervalMinutes);
    }
    else
    {
        LOG_INFO("module", ">> AWS Quiz module is disabled");
    }
}
