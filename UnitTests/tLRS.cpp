/*
 * This file is part of the source code of the software program
 * Vampire. It is protected by applicable
 * copyright laws.
 *
 * This source code is distributed under the licence found here
 * https://vprover.github.io/license.html
 * and in the source directory
 */

#include "Test/MockedSaturationAlgorithm.hpp"
#include "Test/SyntaxSugar.hpp"
#include "Test/UnitTesting.hpp"
#include "Saturation/LRS.hpp"
#include "Kernel/Ordering.hpp"
#include "Shell/Statistics.hpp"

using namespace Test;

namespace {

class IdentitySimplifier : public Inferences::ImmediateSimplificationEngine {
  Clause *simplify(Clause *cl) override { return cl; }
};

// Keep the unprocessed queue nonempty until LRS gets an update opportunity.
// Bound the replacements so a starved callback fails instead of hanging.
class RepeatingSimplifier : public Inferences::ForwardSimplificationEngine {
public:
  explicit RepeatingSimplifier(const bool &updated) : _updated(updated) {}

  bool perform(Clause *cl, Clause *&replacement, ClauseIterator &premises) override
  {
    if (_updated)
      return false;
    ++_replacements;
    ASS_L(_replacements, 1000u);
    replacement = Clause::fromIterator(cl->iterLits(),
        SimplifyingInference1(InferenceRule::EVALUATION, cl));
    return true;
  }

private:
  const bool &_updated;
  unsigned _replacements = 0;
};

class LoopingLRS : public Saturation::LRS {
public:
  LoopingLRS(Problem &problem, Options &options) : LRS(problem, options)
  {
    _immediateSimplifier = new IdentitySimplifier();
    FwSimplList::push(new RepeatingSimplifier(updated), _fwSimplifiers);
  }

  void process(Clause *input)
  {
    addNewClause(input);
    doUnprocessedLoop();
  }

  bool updated = false;

protected:
  void poppedFromUnprocessed() override
  {
    // Observe the scheduling decision without depending on a time estimate.
    updated |= shouldUpdateLimits();
  }
};

}

TEST_FUN(update_during_repeated_simplification)
{
  DECL_DEFAULT_VARS
  DECL_SORT(s)
  DECL_PRED(p, {s})
  auto input = clause({p(x)});
  Problem problem(UnitList::singleton(input));
  env.setMainProblem(&problem);
  resetAndFillEnvOptions({{"avatar", "off"}}, problem);
  env.statistics->activations = 11;
  {
    LoopingLRS algorithm(problem, *env.options);
    algorithm.process(input);
    ASS(algorithm.updated);
  }
  Ordering::unsetGlobalOrdering();
}
