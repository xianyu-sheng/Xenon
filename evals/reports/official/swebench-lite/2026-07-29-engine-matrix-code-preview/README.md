# Archived Code Preview calibration — not a benchmark result

This directory preserves an incomplete Ark/Coding Plan Code Preview batch and
the calibration traces used to diagnose two generic runner defects:

- repeated incompatible native request shapes;
- multiprocessing Queue backpressure while returning large traces.

The batch stopped after the account reached the provider's Safe Experience
Mode inference limit. It has no complete eight-engine denominator and no
official verdict for every engine, so it must not be presented as a benchmark
score. No account limit or paid setting was changed.

The completed eight-engine run using the user's separate DeepSeek official API
credentials is in:

`../2026-07-29-engine-matrix-deepseek-official-v4-pro/`
