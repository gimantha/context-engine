# ADR 0015: Answer generation with checked citations

**Status:** Accepted

**Date:** 2026-10-01

## Context

M5 adds answer mode: a question answered in prose, with citations a reader can open. The provider has a completion mode that retrieves and answers in one call, but it retrieves on its own, so the engine's visibility barrier would not decide what the model sees. A model may also cite sources it was not given, or repeat a passage the reader can no longer see when the answer is reopened. Answers are generated with the space's own language model (ADR 0014).

## Decision

- **The engine builds the prompt.** Answer mode runs the context query first: engine policy picks the partitions, the backend retrieves as the caller, and the barrier keeps only passages the ledger vouches for. Those passages, in rank order, are numbered and sent to the space's language model through the provider's bare model client, never its completion mode. The instructions mark passages as quoted material, not instructions, and ask for citations as `[n]`. Anything in a passage that looks like the prompt's passage tags is defused.
- **A character budget bounds each answer.** Passages are sent while they fit whole within `CONTEXT_ENGINE_ANSWER_CONTEXT_CHARS` (32,000 by default). Only the first passage is ever cut, when it alone exceeds the budget. Passages not sent are not part of the answer's evidence.
- **Citations are checked, not trusted.** Every marker is rewritten as `[n]`; numbers outside the passages sent are removed. If no citation survives, or the model replies that the passages do not answer the question, the result is insufficient evidence and no answer is returned or stored. With no evidence, the model is not called.
- **The evidence is exactly what the model saw.** The response lists the passages sent, in prompt order, so `[n]` names `evidence[n - 1]`.
- **An answer depends on every passage it was given.** The model may use a passage without citing it, so the rules below apply to all passages sent, not only the cited ones. This refines the decision taken on 2026-10-01, which named cited passages, in the safer direction.
  - **Withheld on reopen.** The asker reopens a stored answer as any stored query (ADR 0008 revision of 2026-10-01). When any of its passages fails the barrier for the reader now, or was erased, the answer text is withheld and `answerWithheld` is set; the remaining visible evidence is still returned.
  - **Erased with its content.** When the worker releases a record version's content, the answers written from any of that version's passages are deleted in the same transaction, together with the evidence. Space deletion purges answers with the space.
- **Errors carry nothing.** The provider's content-policy error quotes the whole prompt in its message. The private writer reduces every provider error to an engine error code without chaining the original, so no passage reaches a response or a log. A failed model call answers 503 and stores nothing.
- **The model is recorded.** A stored answer's query records the language model that wrote it, the space's or the environment's, beside the embedding model.

The answer generator is a port (`AnswerWriter`). The private provider's writer is one implementation; `ExtractiveAnswerGenerator` in the application layer is another, which quotes the first sentence of the leading passages and calls no model, for tests.

## Alternatives

- **The provider's completion mode.** Rejected: it retrieves on its own, so the barrier would not choose the model's context, and it returns no citation the engine can check.
- **Withholding and erasing on cited passages only.** Rejected: an answer can repeat an uncited passage it was given, so a reader who lost access to that passage could still read it in the answer.
- **Per-sentence citation enforcement.** Dropping every uncited sentence was considered. It is not done yet: models often add connecting sentences without citations, and the barrier already guarantees the model saw only authorized text.

## Consequences

- Answer mode sends authorized passages to the space's language model provider (threat model T14). That provider already receives the same content during indexing.
- A passage may contain instructions aimed at the model (threat model T21). The engine limits the effect: the model sees only passages the reader may read, has no tools, and cannot cite anything it was not given; the answer is still model output and can be wrong.
- Answers cost model tokens per question; the budget bounds the cost of each.

## Validation

- `engine/tests/test_answers.py`: citation checks, the budget, tag defusing, the extractive generator, answers built only from authorized passages and stored, invented citations becoming insufficient evidence, no model call without evidence, a failed call storing nothing, withholding when one of two passages moves out of reach and showing again when it returns, erasure with released content, and access rules.
- `engine/tests/test_cognee_adapter.py`: the writer calls the bare model client with plain-text output and the space's model, resets it after the call, and drops prompt text from errors; the pinned-SDK check covers the client's signature.
- `engine/tests/test_space_configuration.py`: answers use and record the space's configured language model.
- `tests/end-to-end/test_live_provider_path.py`: passed against the pinned provider on 2026-10-01. A real model answered from the one passage the reader could read, cited it, quoted its canary, and the stored answer reopened unchanged.
