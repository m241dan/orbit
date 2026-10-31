# Testing

## Every test

- Write the test first, and watch it fail for the reason you expect.
- Name each test with a sentence saying what happens under what condition.
- Check behaviour: a test fails when the behaviour it covers breaks. A line that coverage runs
  but no assertion checks is untested.
- Cover every line and branch, each with a test that checks what it does.
- Pin each fix with the test that would have caught it, in the fix's own commit.
- Type tests like the code; tests keep to the same function limits.
- Keep each test independent of the machine and of other tests. Pass in the clock, randomness
  and ids, drive loops by hand, and wait on a gate, a condition or quiet, with a bound on every
  wait.
- Give External's tests local stand-ins: temp dirs, local git repositories, loopback servers.

## Each type

| Type | Test with |
|---|---|
| Domain | Small unit tests. Property-based tests (generate many inputs, check that a rule holds for all of them) wherever a rule should hold across inputs; required for a library's Domain code. |
| Application | Behaviour scenarios, given / when / then, against fakes of External's ports, with Execution run deterministically. |
| External | Medium tests against local stand-ins: temp dirs, loopback sockets, in-memory servers. Each asserts both the translated error and that nothing was left half done. |
| Execution | A deterministic loop or executor and a fake clock; stress tests under sanitizers where the language has them, printing the seed or schedule that replays a failure. |
| Entry | A few start-up tests that build the real wiring in a test configuration and check that it comes up. |

## Sizes

- **Small**: one process, no I/O, no sleeps. Domain, Application and Execution's deterministic
  tests. They run in every task's checks.
- **Medium**: localhost and temp dirs. External and Entry. They run in every task's checks while
  they stay fast.
- **Large**: everything else, such as stress runs and many nodes. They run in a later check set.

## Fakes

- Give each port an in-memory fake, and test Application against it. A fake keeps a test about
  behaviour; a mock of a library makes it about calls.
- Every fake passes the same contract tests as the adapter it stands in for, so the two can't
  drift apart.
- Test third-party code through External's own tests, against local stand-ins.
- Test through public behaviour. When a test must look inside, it uses accessors kept in one
  helper module.

## Mutation testing

Mutation testing makes small changes to the code, such as flipping a comparison, and checks that
some test fails. It covers Domain and Application code and runs in a later check set. A
surviving mutant names a behaviour no test checks: add the test. A mutant that can't change
behaviour gets the tool's skip marker, where a reviewer sees it.

## Fuzzing

Code that parses untrusted input gets fuzz targets: libFuzzer in C++, Hypothesis strategies in
Python. Long runs go in a later check set.
