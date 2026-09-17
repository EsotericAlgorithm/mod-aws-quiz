/*
 * mod-aws-quiz
 */

#ifndef _AWS_QUIZ_CONFIG_H_
#define _AWS_QUIZ_CONFIG_H_

#include "Define.h"

class AwsQuizConfig
{
public:
    static AwsQuizConfig* instance();

    void LoadConfig();

    bool IsEnabled() const { return _enabled; }
    uint32 GetPollIntervalMs() const { return _pollIntervalMs; }
    uint32 GetMinIntervalMinutes() const { return _minIntervalMinutes; }
    uint32 GetMaxIntervalMinutes() const { return _maxIntervalMinutes; }
    uint32 GetQueueTimeoutSeconds() const { return _queueTimeoutSeconds; }

private:
    AwsQuizConfig() = default;
    ~AwsQuizConfig() = default;

    bool _enabled = false;
    uint32 _pollIntervalMs = 2000;
    uint32 _minIntervalMinutes = 20;
    uint32 _maxIntervalMinutes = 30;
    uint32 _queueTimeoutSeconds = 120;
};

#define sAwsQuizConfig AwsQuizConfig::instance()

#endif // _AWS_QUIZ_CONFIG_H_
