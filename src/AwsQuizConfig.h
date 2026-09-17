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

    bool IsStreakBuffEnabled() const { return _streakBuffEnabled; }
    uint32 GetStreakBuffDamageSpellId() const { return _streakBuffDamageSpellId; }
    uint32 GetStreakBuffCritSpellId() const { return _streakBuffCritSpellId; }
    uint32 GetStreakBuffDurationMinutes() const { return _streakBuffDurationMinutes; }
    uint32 GetStreakBuffPercentPerStack() const { return _streakBuffPercentPerStack; }
    uint32 GetStreakBuffMaxStacks() const { return _streakBuffMaxStacks; }

private:
    AwsQuizConfig() = default;
    ~AwsQuizConfig() = default;

    bool _enabled = false;
    uint32 _pollIntervalMs = 2000;
    uint32 _minIntervalMinutes = 20;
    uint32 _maxIntervalMinutes = 30;
    uint32 _queueTimeoutSeconds = 120;

    // "Streak" buff: a correct answer adds a stack (refreshing duration);
    // a wrong answer clears it entirely. Host spells were picked because
    // each has exactly one aura effect of the needed type (Enrage =
    // SPELL_AURA_MOD_DAMAGE_PERCENT_DONE, Rampage =
    // SPELL_AURA_MOD_WEAPON_CRIT_PERCENT) — their own DBC magnitude/stack
    // cap is irrelevant, AwsQuizStreakBuff.cpp overrides the effect amount
    // directly and stacks are set via Aura::SetStackAmount(), which (unlike
    // ModStackAmount()) isn't clamped by the DBC's own StackAmount field.
    bool _streakBuffEnabled = true;
    uint32 _streakBuffDamageSpellId = 12880;  // Enrage
    uint32 _streakBuffCritSpellId = 29801;    // Rampage
    uint32 _streakBuffDurationMinutes = 5;
    uint32 _streakBuffPercentPerStack = 5;
    uint32 _streakBuffMaxStacks = 10;
};

#define sAwsQuizConfig AwsQuizConfig::instance()

#endif // _AWS_QUIZ_CONFIG_H_
