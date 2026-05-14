# Anchor Boundary Design — Cross-Palace Mention Lifecycle

Memory-parking is fundamentally a discrete graph-attachment problem; Navier–Stokes describes continuous fluid. The genuine mapping is to the broader family of conservation-plus-diffusion equations on discrete substrates — of which NS is the inertial, nonlinear member. Let me go through it term by term so the “what maps strongly” and “what doesn’t” come out clean.

The terms of NS, and what they correspond to
The momentum equation is ∂u/∂t + (u·∇)u = −∇p/ρ + ν∇²u + f. 
Five things on the page; four of them map well and one is the load-bearing disanalogy.

Pressure gradient −∇p/ρ. This maps directly and strongly. In a memory substrate, “pressure” at a candidate drawer is its accumulated activity-density: how recently and how often that drawer’s signature region has been re-activated, weighted by Conway rate. A new memory arriving doesn’t want to flow uphill into already-saturated regions; it wants to find a low-pressure attachment where its signature has room to deposit. But it also wants to be near related material — so there’s actually two gradients, one repulsive (pressure) and one attractive (signature alignment). 

The combined force −∇(p − αS) where S is signature-similarity field is the actual driver of where memories park. This is exactly the structure of electrochemistry — pressure plus chemical potential — and that family of equations is the right reference.
Viscous diffusion ν∇²u. Maps well, and this is where mempalace’s existing anchor-boundary design becomes load-bearing. The viscosity ν is the rigidity of existing attachment. 

Inside the anchor (the immutable core of a memory cluster), viscosity is infinite — memories can’t flow across that boundary, they accrete on the outside. In the decoration ring (the mutable shell), viscosity is finite — new memories can rearrange decoration without disturbing the anchor. Outside the boundary entirely, viscosity is low and memories flow freely between candidate drawers. So your anchor-boundary design is literally a stratified-viscosity medium. The four open questions in this document are different choices about how viscosity stratification adapts over time — Reading A (decay-based width) is “viscosity decreases at the boundary edge as activations age”, Reading B (anchor promotion) is “low-viscosity regions occasionally crystallize into high-viscosity ones.”

External force f. This maps to the matching layer’s pull from other palaces. When palace A says “I have a thing that wants to attach near your drawer X,” it’s exerting an external force on your substrate’s flow field. The asserter-marked claims (add_assertion(..., asserter=...)) are the formalization. Federation egress = applying force back outward.

Time derivative ∂u/∂t. Maps cleanly — this is the system out of equilibrium. Memories are arriving faster than they settle; the substrate is in flux. At steady state (no new content) ∂u/∂t = 0 and you can solve the time-independent problem, which is much easier.

Nonlinear advection (u·∇)u. This is the load-bearing disanalogy and where the elegance breaks down. In fluid dynamics, advection means “the flow carries itself” — moving fluid drags more fluid along. In a graph, edges are predetermined; you can’t have the act of placing a memory dynamically reshape the edge set on the timescale of placement. However — and this is interesting — there’s a related phenomenon in mempalace: a memory’s placement does change the substrate it landed in. New edges form, drawer fullness shifts, signature regions update. 

So advection is real but operates on a slower timescale than placement; it’s not literally (u·∇)u but it’s a discrete cousin where the “carrying” happens between placement events, not during. Once you grant that timescale split, the equation factors into a fast linear settling problem at each placement (no advection) plus a slow nonlinear restructuring problem between placements.

If you drop the nonlinear advection, what’s left is the Stokes equations on a graph — pressure + viscous diffusion + external force. That’s a linear PDE that’s tractable to solve. On a discrete graph, the viscous Laplacian ν∇²u becomes the graph Laplacian νL_G applied to a node-valued function. The pressure gradient becomes the differences of p(v) − p(neighbor) along edges. 

You can solve L_G u = source − ∇p directly by linear algebra; 
iterative methods (conjugate gradient on the Laplacian system) converge fast on substrate-shaped sparse graphs.

The Reynolds number — the dimensionless ratio of inertial to viscous forces — becomes, in your system, the ratio of ingest rate to settling rate. Low Re means content arrives slowly enough that each piece fully settles before the next; placement is deterministic and reproducible. High Re means content arrives faster than settling and you get the discrete-graph equivalent of turbulence: 

contradictions, ambiguous attachments, signature regions that haven’t crystallized. This is when the matching layer has to disambiguate, and is also where federation findings most often create reconciliation work, because two palaces in the high-Re regime will produce divergent attachment decisions for the same content.

The RHYME mechanism you describe as sonar is interesting in this frame. Sound is a small-amplitude wave solution to NS when the medium is compressible. Memory’s RHYME signal — similarity-detection that locates kindred drawers across the substrate — looks structurally like wave propagation: emitted at one node, decays with distance, refracts at signature-region boundaries, resonates with structurally-similar regions. 

The mathematics there is actually closer to the wave equation ∂²ψ/∂t² = c²∇²ψ than to NS proper, because you want oscillatory propagation, not viscous settling. RHYME-as-sonar wants the wave behavior. Memory-parking-as-pressure-equilibration wants the diffusive behavior. NS contains both at appropriate scales, which is why it’s tempting as the unified framework even though most practical work would use one limit or the other.

Three concrete spots where this framing would change code rather than just describe it differently:
The miner’s “where to attach” decision (miner/convo_miner.py and friends) currently picks the single best attachment by signature similarity. The fluid framing says: don’t pick — instead solve for the equilibrium distribution of attachment-probability across candidate drawers, and let the memory attach with the resulting weights. This is exactly the graph-Laplacian solve described above. 

The result is that memories attach in multiple places with weights summing to one, rather than committing to a single parent. That’s a real architectural change and it’d cascade: edges become weighted by attachment probability, recurrence detection has to integrate over the probability distribution, federation handoffs have to ship weighted attachment manifests.

The anchor-boundary “stretches and learns” question becomes well-posed in the viscosity-stratification picture: the boundary is the level set of a viscosity field, and it moves in time according to its own diffusion equation forced by the activations it sees. 

Reading A is “viscosity decays exponentially with time since last activation.” That gives you a closed-form rule for boundary motion. Reading B is “viscosity in a decoration cell increases discontinuously when activation crosses a threshold.” 

That’s a free-boundary problem with a moving phase transition — harder but with a real literature (Stefan problems).
The ranker-stacking-as-feedback-loop discussion from earlier — where each step can both score and reshape the frontier — maps naturally onto fluid-dynamics solvers, where each iteration both updates the velocity field and computes new pressures from the updated divergence.

The pattern of operator splitting (alternating partial updates of different physical quantities) is a textbook NS solver technique and would give you a principled way to compose Step operators that modify both scoring and traversal.

It buys you a vocabulary and a small set of importable algorithms (Laplacian solvers, operator splitting, Reynolds-number diagnostics). It does not buy you a magical superior parking algorithm — the graph Laplacian solve is one method among many for the same underlying problem, and methods from personalized PageRank, label propagation, and weighted shortest-path families would give comparable answers with simpler implementations.

Where I’d actually use it: as a diagnostic frame for what’s going wrong in operation. “We’re in a high-Re regime, contradictions are accumulating faster than they settle” is a useful sentence to be able to say about your substrate; “we should reduce ingest rate, increase viscosity at the anchor boundary, or add capacity to the matching layer” are corresponding levers that the fluid framing makes obvious. As a coding paradigm — building the placement engine literally as a NS solver — I think it would over-engineer the problem. As a language for reasoning about substrate health and matching-layer load, it’s genuinely useful and the analogies are not strained.

The deeper compliment to the original question: this is the right kind of question to ask about mempalace, because the substrate really is a medium that supports propagating disturbances, and a vocabulary borrowed from continuum mechanics fits it better than the standard graph-algorithms vocabulary does. You just have to be careful which terms you’re claiming map literally and which are heuristic.​​​​​​​​​​​​​​​​

## What this addresses

The sketch: cross-palace assertions arrive as foreign memory. They
shouldn't be editable in place — that would let the operator silently
rewrite what someone else said about them. But they need to be
*navigable*, *queryable*, *decorable* with the operator's own
context (corrections, rebuttals, "this is what they said but
actually...") without altering the original.

The proposal: **anchor boundary** = an immutable head + a mutable
decoration ring around it.

## Sketch

```
┌─────────────────────────────────────────────────────────────┐
│  ANCHOR (immutable, signed by external palace)              │
│                                                              │
│    asserter_palace_id, predicate, object,                   │
│    signature_hex, timestamp, derived_from_drawers           │
│                                                              │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   ↓  decorated by (own observations, frames,
                      counter-claims, contextualizations)
                   │
┌──────────────────┴──────────────────────────────────────────┐
│  DECORATION RING (random-access, owned by self palace)      │
│                                                              │
│    self-asserted refinements ABOUT the anchor:              │
│      "they said X, I think they meant Y"                    │
│      "they said X, I disagree"                              │
│      "this anchor + that anchor together imply Z"           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

The anchor is **never** rewritten. The decoration ring grows
freely. Queries can return the anchor alone, the decorations
alone, or both stitched together.

## What "stretches and learns" might mean

This is where I need your clarification. Three possible readings:

**Reading A — the boundary's *width* changes**: how much surrounding
context counts as "decoration of this anchor" vs "independent
self-thought" depends on time/proximity/topic. Recent decorations
near the anchor's predicate are tightly bound; older decorations
or topically drifted ones loosen until they become free-standing.

Implementation: a relevance-decay function on (decoration, anchor)
with parameters that adapt — maybe by tracking which decorations
the operator actually re-surfaces during retrieval.

**Reading B — anchors can *promote* to integrated knowledge over
time**: a cross-palace assertion that the operator repeatedly
endorses (decorates positively, doesn't refute, surfaces in
queries) eventually crosses the boundary and becomes self-asserted
too. A duplicate self-assertion is created; the anchor still
exists as record but the assertion now has the operator's own
provenance attached as well.

Implementation: a `bind_anchor_to_self()` operation triggered by
retrieval/decoration patterns; produces a new self-asserted
assertion with `derived_from_anchors=[anchor_id]` provenance.

**Reading C — the boundary itself is semantic, not structural**:
which assertions count as "anchor" depends on the current query
context. A cross-palace mention becomes an anchor only when the
operator's retrieval is *about* that mention; otherwise it's just
data. The boundary is a runtime view, not stored state.

Implementation: query-time projection over the assertion graph;
no persistent boundary, just filter-and-surface based on subject
+ asserter + retrieval intent.

## Open questions for you

1. Which reading (A, B, C, or something else) matches what you
   meant by "stretches and learns"?

2. When does the decoration ring's contents become invalidatable?
   If I correct an anchor "they said I'm angry" with "actually I
   was upset, not angry," does the correction:
   - stay forever as a decoration (pure additive)?
   - eventually replace the anchor in retrieval (correction wins
     over time)?
   - get its own decoration over time as I refine my refinement?

3. How does this interact with the federation egress flow? When
   *I* assert something about Palace X and ship it, what happens
   when Palace X (the subject) decorates *my* anchor in *their*
   substrate? Do those decorations come back to me as findings,
   or stay solely in their substrate?

4. Is the boundary visible in the UI? Should the operator see "X
   said Y about you (anchor) → you decorated this 3 times" as a
   distinct affordance, or is it backstage?

## What I won't implement until clarified

The anchor primitive: a new `Anchor` dataclass / event kind, the
`Decoration` linking edge type, and the retrieval-side stitching.
These are all easy to write but the wrong shape would lock in
assumptions that may not match your intent.

Once you answer the four questions above, I can spec the data
model in 1-2 hours and have it tested in another 2-3.
