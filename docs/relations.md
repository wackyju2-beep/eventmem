# Relations and edge building / 关系与连边

Events record decisions, attempts, results, changes and lessons. Relations capture how a later event relates to an earlier one; a missing edge does **not** mean no relationship exists. The names stored in SQLite are Chinese.

| Stored name | Reading label | Meaning |
|---|---|---|
| 改变 | change | A later event replaces a value, object or condition in the earlier event with a new one of the same kind. |
| 否定 | negate | A later event overturns or retires the earlier one without a substitute of the same kind. |
| 重复 | repeat | The same specific fact, decision or action is restated without substantive new information. |
| 因为 | because | The later event explains why the earlier event happened or was chosen. |
| 细化 | refine | The later event adds concrete execution details to the earlier event. |

The conceptual timeline runs from old to new. In the database, however, `edges.src` is the **later** event and `edges.dst` the **earlier** event. Check the actual API contract before writing a link; don't infer column direction from a diagram.

## How automatic links are selected

The worker narrows candidate pairs instead of comparing every event to every other event. Depending on available data and settings, candidates include vector-similar active heads, recent events from the same session, events sharing an artifact or source path, and selected cross-project or project-history candidates. The exact limits and thresholds are configurable; see [settings](settings.md) and `core/eventmem_core/nightly.py`.

The decision backend evaluates two axes: repeat/change/negate/none and because/refine/none. It can reject both. Acceptance considers the raw answer distribution, a margin over the runner-up, and relation-specific confidence settings. `edges.probs` retains the distribution behind an edge; `pair_evals` archives evaluated pairs, including rejected ones. The graph can expose these judgments for inspection and manual correction. Human edits and deletion are different from appending an event: history is not an immutable audit log.

An accepted `重复` suppresses the second axis; manually created edges do not necessarily have a model probability distribution.

The model, prompt, candidate selection and thresholds all affect the result. An edge is a stored judgment, **not proof**. A conservative policy can reduce unsupported links while also missing valid ones; edge rate alone is not an accuracy metric. Evaluate both false positives and false negatives against human-labelled pairs before making accuracy claims. Retrieval without an edge remains possible but may not reveal that the facts are related.

Historical thresholds can be recalibrated from saved distributions, subject to the safeguards described in [recalibration safety](recalibration-safety.md). [Backends](backends.md) documents model setup and the available local, external and remote decision services.

## Historical measurement

An earlier version was measured on a six-core mini PC without a GPU using a 4B decision model quantised to Q8_0 (4.48 GB): approximately 6.7 seconds per pair on six threads, or 16–18 seconds on two threads. This is a historical observation, not a current benchmark or a guarantee for other models, prompts, versions or hardware.
