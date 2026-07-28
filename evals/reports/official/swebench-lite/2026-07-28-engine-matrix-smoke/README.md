# SWE-bench Lite engine-matrix smoke (partial)

This directory records separate engine attempts on the unchanged official
`astropy__astropy-12907` task. It is deliberately labelled **partial**: provider
429s and read timeouts prevented all eight modes from completing in one batch.
Missing modes are not assigned a score.

| Engine | Model | Patch | Official outcome | Tool execution |
| --- | --- | ---: | --- | ---: |
| direct | DeepSeek V4 Pro | empty | official `empty_patch`; no instance run | N/A |
| react | DeepSeek V4 Pro | 497 bytes | `resolved = true` | 7/9 succeeded |
| plan-execute | DeepSeek V4 Pro | empty | official `empty_patch`; no instance run | 3/3 succeeded |
| plan-react | DeepSeek V4 Flash | empty | official `empty_patch`; no instance run | N/A |
| plan-reflection | DeepSeek V4 Flash | empty | provider 429; not scored | N/A |
| reflection | DeepSeek V4 Pro | incomplete | provider read timeout; not scored | N/A |
| react-reflection | — | not completed | not scored | N/A |
| novel | — | not completed | not scored | N/A |

The official reports do not call empty patches unresolved; they list one
`empty_patch` and run zero instances. Accordingly, no denominator is invented
for those modes.

## Separate telemetry

The completed V4 Pro attempts reported provider evidence as follows:

| Engine | Prompt tokens | Completion tokens | Cache-hit tokens | Cache hit rate |
| --- | ---: | ---: | ---: | ---: |
| direct | 483 | 244 | 0 | 0% |
| react | 1,179 | 794 | 0 | 0% |
| plan-execute | 3,601 | 621 | 0 | 0% |

No token or cost saving is claimed: cache-hit tokens were zero and no pricing
contract was supplied to the adapter. Correctness, tool success, cache reuse,
and cost remain separate fields.
