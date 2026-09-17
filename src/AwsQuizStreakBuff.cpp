/*
 * mod-aws-quiz
 *
 * The "streak" buff: each correct answer adds a stack of +N% damage done
 * and +N% weapon crit chance, refreshing a fixed duration; a wrong answer
 * clears every stack immediately. WoW's percent-damage/crit bonuses only
 * exist as aura effects tied to a real spell.dbc entry (a module can't
 * invent a new one without client-side DBC patching), so this reuses two
 * existing, unrelated Blizzard spells purely as effect-type "carriers":
 *
 *   - Enrage (12880): a single SPELL_AURA_MOD_DAMAGE_PERCENT_DONE effect.
 *   - Rampage (29801): a single SPELL_AURA_MOD_WEAPON_CRIT_PERCENT effect.
 *
 * Verified in-game via `.spellinfo effects <id>` before picking these —
 * both have exactly one effect, so overriding that effect's amount below
 * is the whole story, no other side effects to worry about. Their own
 * DBC-declared magnitude and StackAmount are irrelevant: DoEffectCalcAmount
 * overrides the magnitude every recalculation, and Aura::SetStackAmount()
 * (unlike ModStackAmount()) isn't clamped by the DBC's StackAmount field,
 * so an arbitrary stack count/cap is fully controlled by config here.
 */

#include "AwsQuizStreakBuff.h"
#include "AwsQuizConfig.h"

#include "Player.h"
#include "SpellAuraEffects.h"
#include "SpellAuras.h"
#include "SpellScript.h"
#include "SpellScriptLoader.h"

#include <algorithm>

class spell_aws_quiz_streak_damage : public AuraScript
{
    PrepareAuraScript(spell_aws_quiz_streak_damage);

    void CalculateAmount(AuraEffect const* aurEff, int32& amount, bool& /*canBeRecalculated*/)
    {
        amount = static_cast<int32>(sAwsQuizConfig->GetStreakBuffPercentPerStack())
               * static_cast<int32>(aurEff->GetBase()->GetStackAmount());
    }

    void Register() override
    {
        DoEffectCalcAmount += AuraEffectCalcAmountFn(spell_aws_quiz_streak_damage::CalculateAmount, EFFECT_0, SPELL_AURA_MOD_DAMAGE_PERCENT_DONE);
    }
};

class spell_aws_quiz_streak_crit : public AuraScript
{
    PrepareAuraScript(spell_aws_quiz_streak_crit);

    void CalculateAmount(AuraEffect const* aurEff, int32& amount, bool& /*canBeRecalculated*/)
    {
        amount = static_cast<int32>(sAwsQuizConfig->GetStreakBuffPercentPerStack())
               * static_cast<int32>(aurEff->GetBase()->GetStackAmount());
    }

    void Register() override
    {
        DoEffectCalcAmount += AuraEffectCalcAmountFn(spell_aws_quiz_streak_crit::CalculateAmount, EFFECT_0, SPELL_AURA_MOD_WEAPON_CRIT_PERCENT);
    }
};

static void ApplyOrRefreshOne(Player* player, uint32 spellId, int32 durationMs, uint32 maxStacks, uint32& outStack)
{
    if (Aura* aura = player->GetAura(spellId))
    {
        uint32 newStack = std::min<uint32>(static_cast<uint32>(aura->GetStackAmount()) + 1, maxStacks);
        aura->SetStackAmount(static_cast<uint8>(newStack));
        aura->SetMaxDuration(durationMs);
        aura->SetDuration(durationMs);
        outStack = newStack;
    }
    else if (Aura* newAura = player->AddAura(spellId, player))
    {
        newAura->SetStackAmount(1);
        newAura->SetMaxDuration(durationMs);
        newAura->SetDuration(durationMs);
        outStack = 1;
    }
    else
    {
        outStack = 0;
    }
}

void AwsQuizApplyOrRefreshStreakBuff(Player* player, uint32& outStackCount)
{
    outStackCount = 0;
    if (!player || !sAwsQuizConfig->IsStreakBuffEnabled())
        return;

    int32 durationMs = static_cast<int32>(sAwsQuizConfig->GetStreakBuffDurationMinutes()) * MINUTE * IN_MILLISECONDS;
    uint32 maxStacks = std::max<uint32>(1, sAwsQuizConfig->GetStreakBuffMaxStacks());

    uint32 dmgStack = 0;
    uint32 critStack = 0;
    ApplyOrRefreshOne(player, sAwsQuizConfig->GetStreakBuffDamageSpellId(), durationMs, maxStacks, dmgStack);
    ApplyOrRefreshOne(player, sAwsQuizConfig->GetStreakBuffCritSpellId(), durationMs, maxStacks, critStack);

    // Kept in lockstep by construction (both applied/refreshed together
    // every time) — either is representative of "the" current streak.
    outStackCount = dmgStack;
}

void AwsQuizClearStreakBuff(Player* player, bool& outHadStreak)
{
    outHadStreak = false;
    if (!player || !sAwsQuizConfig->IsStreakBuffEnabled())
        return;

    if (player->GetAura(sAwsQuizConfig->GetStreakBuffDamageSpellId()) ||
        player->GetAura(sAwsQuizConfig->GetStreakBuffCritSpellId()))
        outHadStreak = true;

    player->RemoveAura(sAwsQuizConfig->GetStreakBuffDamageSpellId());
    player->RemoveAura(sAwsQuizConfig->GetStreakBuffCritSpellId());
}

void AddAwsQuizStreakBuffScripts()
{
    RegisterSpellScript(spell_aws_quiz_streak_damage);
    RegisterSpellScript(spell_aws_quiz_streak_crit);
}
