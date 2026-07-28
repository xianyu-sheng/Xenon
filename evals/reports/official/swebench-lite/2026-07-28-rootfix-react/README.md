# Root-fix official runtime smoke

This is one infrastructure acceptance run, not a SWE-bench Lite score and not
an all-engine matrix result.

- Dataset: unchanged `SWE-bench/SWE-bench_Lite` test split
- Instance: `astropy__astropy-12907`
- Model/engine: DeepSeek V4 Flash + Xenon ReAct
- Agent environment: official instance image
  `swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest`
- Image content ID:
  `sha256:f3f63bb87d581c0e7b47f900dd82165b71040e1758d3c29e915e2b18da9baf63`
- Grader: unchanged `swebench==4.1.0` official Docker harness

Observed result:

```text
Official resolved:              1/1
Real tool execution success:    8/8
Result assertion (official):    pass
Patch source:                   workspace
Patch bytes:                    504
Agent elapsed time:             45.869 s
Provider request start/end:     8/8 (balanced)
Unstopped containers:           0
```

The command tool was separately verified inside the same bind-mounted
`/testbed` with the official `testbed` Conda environment (`erfa==2.0.0.3`).
File tools edited the host side of that mount, while command and git tools ran
inside the container. The agent runtime did not receive `eval.sh`, the test
patch, or a reference patch. Those grader-only inputs were introduced later
by the official harness.

Provider cache evidence was absent for this run, so no cache hit, saved-token,
or saved-cost claim is made. A successful single smoke also says nothing about
the 300-instance verified success rate or the other Xenon engines.
