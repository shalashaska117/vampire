"""Separate solver answers from instrumentation failures."""
import re

SANITIZER = re.compile(r'(?:ERROR: (?:AddressSanitizer|LeakSanitizer|MemorySanitizer|ThreadSanitizer):[^\n]*|'
                       r'SUMMARY: (?:AddressSanitizer|LeakSanitizer|MemorySanitizer|ThreadSanitizer):[^\n]*|'
                       r'[^\n]*runtime error:[^\n]*|AddressSanitizer:DEADLYSIGNAL)')
ASSERTION = re.compile(r'Assertion violation|Condition at location [^\n]* violated:')


def sanitizer_messages(stdout, stderr):
    return list(dict.fromkeys(SANITIZER.findall(stdout + '\n' + stderr)))


def combine(semantic_outcome, semantic_reason, sanitizers, errors, timed_out):
    actual = [e for e in errors if e['kind'] not in ('invalid-xml', 'missing-xml')]
    if sanitizers or actual:
        memory = 'fail'
    elif errors or timed_out:
        memory = 'inconclusive'
    else:
        memory = 'pass'
    if memory == 'fail':
        return 'fail', ('sanitizer reported errors' if sanitizers else 'Valgrind reported errors'), memory
    if semantic_outcome == 'fail': return semantic_outcome, semantic_reason, memory
    if memory == 'inconclusive': return 'inconclusive', semantic_reason or 'incomplete memory diagnostics', memory
    return semantic_outcome, semantic_reason, memory
