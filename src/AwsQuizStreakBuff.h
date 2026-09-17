/*
 * mod-aws-quiz
 */

#ifndef _AWS_QUIZ_STREAK_BUFF_H_
#define _AWS_QUIZ_STREAK_BUFF_H_

#include "Define.h"

class Player;

// Adds a stack (or applies fresh) and refreshes the streak buff's
// duration. outStackCount is set to the resulting stack count (0 if the
// buff is disabled or the player pointer is null).
void AwsQuizApplyOrRefreshStreakBuff(Player* player, uint32& outStackCount);

// Fully removes the streak buff. outHadStreak is true if a streak was
// actually active before this call (so callers can skip an empty "streak
// lost" message when there was nothing to lose).
void AwsQuizClearStreakBuff(Player* player, bool& outHadStreak);

void AddAwsQuizStreakBuffScripts();

#endif // _AWS_QUIZ_STREAK_BUFF_H_
