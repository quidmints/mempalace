# The Triples Reframe

`Graph.assert_triple` was renamed to `Graph.add_assertion` in Track 1. Vestigial
naming sweep aside, the rename was correct: the substrate's data unit is the
**8-part assertion frame** (R3 §1.3), not the W3C SPO triple.

But "triples" wasn't *wrong* — it was under-specified. The user-stated reframe:

> Triples are being renamed but that doesn't mean the concept of triples is
> gone all-together, we have to recalibrate what that means. There is the
> triangulation sense, and it's an instance of things that are paired
> together... might be a couple, a quintuple, or a triple... these are edges
> that associate nodes in the DAG.

What "triple" was reaching for, in two distinct senses:

## Sense 1 — Variable-arity associations (the n-tuple sense)

An edge in the DAG associates one source node with one target node, plus
typed kind / valid-time / weight / confidence / derivation. That's the
binary case. Higher-arity associations are expressed as patterns OVER edges:

  - 2-tuple (couple): a single edge between two nodes.
  - 3-tuple (triple): a hyper-edge (three nodes co-related); modeled as
    an assertion node with three `asserted_*` edges (subject, object, +
    one more — typically scope or context).
  - 5-tuple (quintuple): an assertion node with five outgoing edges. The
    standard frame for prediction-market formula expansion: subject /
    predicate / object / time / source.
  - n-tuple: any pattern of edges sharing a common assertion node.

The DAG already supports this via the assertion-node-with-edges shape; what
was missing was the language to talk about higher-arity tuples as
first-class. Use cases:

  - **Quintuples** for formulas: `(subject, predicate, object,
    time, source)` is the natural prediction-market shape (R3 §3.4).
  - **Triples for triangulation** (sense 2 below).
  - **Pairs for simple co-occurrence**: e.g., "Alice mentioned Bob" = one
    edge from an assertion to two entity nodes.

## Sense 2 — Triangulation (the agreement sense)

Three independent palaces all asserting the same claim about a subject is
qualitatively different from one palace asserting it three times. The
"triple" in this sense is **three independent witnesses**, not three nodes.

This is structurally what the matching layer does:

  - Palace A self-asserts "I am thoughtful" (one assertion, one asserter).
  - Palace B externally asserts "Alice is thoughtful" (one assertion,
    different asserter).
  - Palace C externally asserts "Alice is thoughtful" (one assertion,
    yet another asserter).

The cross-palace agreement count = **3** is the triangulation signal. Code
already supports this via the asserter field added to assertion nodes
(see `mempalace/views/graph.py` for `AssertionAsserter` and the
`assertions_about_self()` query helper).

## Glossary updates

The following names should be used going forward:

| Old / informal | New | Sense |
|---|---|---|
| "triple" | **assertion** (8-part frame) | The data unit |
| "triple-store" | **assertion graph** | Storage shape |
| "triple of witnesses" | **triangulation** (n=3) | Cross-palace agreement |
| "n-tuple of edges" | **higher-arity association** | Variable-arity edges |

When code or docs say "triple," check which sense is meant and rename
accordingly. Vestigial uses in the codebase (4 known: a batch-label
string, three doc comments) are scheduled for cleanup but don't carry
meaning.

## Code locations

- `mempalace/views/graph.py` — `Graph.add_assertion` is the canonical
  entry point. `AssertionAsserter` carries the asserter identity for
  triangulation.
- `mempalace/views/graph.py` — `assertions_about_self()` and
  `external_mentions_of_self()` surface the triangulation count.
- `mempalace/federate/anchor_boundary.py` — `decorations_for_anchor()`
  is the "stretch the boundary" reading of the triangulation evidence
  that has accumulated.
