# Attempt 1: kept as found

This was a real-model run. It completed and exercised the whole path: the refinement agent, the reviewer, a story
revision, and the Architect consuming the process context. It showed one defect: findings were phrased as gap
descriptions ("No audit trail requirement ..."), so applying them produced unusable story text
("Control: No audit trail requirement ...").

The fix: both prompts now ask for the exact sentence to add to the story (commit after `1eccd7a`). The demonstration
was run again once; see `../process_context_run/`.
