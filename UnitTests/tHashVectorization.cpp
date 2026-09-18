/*
 * This file is part of the source code of the software program
 * Vampire. It is protected by applicable
 * copyright laws.
 *
 * This source code is distributed under the licence found here
 * https://vprover.github.io/license.html
 * and in the source directory
 */
#include <bit>

#include "Lib/Hash.hpp"
#include "SAT/SATClause.hpp"
#include "Test/UnitTesting.hpp"

using namespace Lib;
using namespace SAT;

// Keep the length unknown inside these loops so the optimizer cannot replace
// the regression with a constant-size specialization.
#if defined(__GNUC__) || defined(__clang__)
__attribute__((noinline))
#endif
static unsigned hashWords(const int* values, unsigned length)
{
  unsigned hash = 0;
  for (unsigned i = 0; i < length; ++i)
    hash ^= FnvHash::hash(values[i]);
  return hash;
}

#if defined(__GNUC__) || defined(__clang__)
__attribute__((noinline))
#endif
static unsigned hashClause(const SATClause& clause)
{
  return SATClauseHash::hash(clause);
}

TEST_FUN(vectorizedFnvMatchesReference)
{
  static_assert(sizeof(int) == 4);
  const int values[] = {104730, -112649, 120568, -128487, 136406, -144325};
  // Precomputed FNV-1a prefix XORs: an optimized runtime reference can suffer
  // from the same compiler bug as the code under test.
  const unsigned little[] = {0u, 3250545605u, 3478552781u, 4283298335u, 712790998u, 11500443u, 4018666989u};
  const unsigned big[] = {0u, 2377339961u, 3795687967u, 2421540907u, 3433883646u, 659150899u, 1491540703u};
  static_assert(std::endian::native == std::endian::little ||
                std::endian::native == std::endian::big);
  const unsigned* expected = std::endian::native == std::endian::little ? little : big;

  for (unsigned length = 0; length <= 6; ++length) {
    auto* clause = new(length) SATClause(length);
    for (unsigned i = 0; i < length; ++i)
      (*clause)[i] = SATLiteral(values[i]);
    ASS_EQ(hashWords(values, length), expected[length]);
    ASS_EQ(hashClause(*clause), expected[length]);
    clause->destroy();
  }
}
