# bsg_cache

## Purpose
A configurable set-associative write-back cache with a DMA-style fill/flush interface.
Accepts byte-addressed memory packets on the core-side request port, returns one data
word per request on the core-side response port, and issues block-aligned fills /
evictions on the memory-side DMA port on misses.

## Parameters (selected)
- `addr_width_p`        — full byte address width.
- `data_width_p`        — word data width.
- `block_size_in_words_p` — words per cache line; must be ≥ 1. When the value is `1`
  the line is a single word and the block-offset field has zero meaningful bits.
- `sets_p`, `ways_p`    — sets and ways of the tag array.
- `dma_data_width_p`    — DMA bus payload width.
- `amo_support_p`       — bitmap of implemented atomic primitives.

## Ports (high-level groupings)
- Core request:  `v_i`, `ready_o`, `cache_pkt_i`, `cache_pkt_v_i`, `cache_pkt_yumi_o`.
- Core response: `data_o`, `v_o`, `yumi_i`.
- DMA: `dma_pkt_o`, `dma_pkt_v_o`, `dma_data_i`, `dma_data_v_i`, `dma_data_o`,
  `dma_data_v_o`.

## Behavioral Contract
- A request commits at the input handshake when `v_i & ready_o`; from there it propagates
  through pipeline stages (TL, V, M, ...) and either hits in the tag array or misses,
  in which case the pipeline stalls until the DMA fill completes.
- The data RAM is addressed by three concatenated fields derived from the request
  address: tag (upper), set-index (middle), block-offset within a line (lower).
- The block-offset field is determined by `block_size_in_words_p`:
  - When `block_size_in_words_p > 1`, it is the address slice
    `addr_v_r[lg_data_mask_width_lp +: lg_block_size_in_words_lp]`.
  - When `block_size_in_words_p == 1`, it is the constant `0` — the line holds a single
    word and the block-offset has zero meaningful bits.
- Tag-hit detection is per-way: `tag_hit_v[i]` is asserted when way `i`'s stored tag
  equals the request tag AND that way's valid bit is set.

## Invariants
- The block-offset value is conditional on `block_size_in_words_p`: a positive-width
  address slice when `block_size_in_words_p > 1`, and a constant `0` otherwise.
- The address-field decomposition consumed by any committed core response matches the
  decomposition presented at the corresponding earlier handshake.
- Atomic primitives commit as a fused load-modify-store; the cache cannot claim
  "atomic completed" while still holding intermediate state.
- Eviction issues a DMA write of the dirty victim before any fill that would overwrite
  it.
- After reset, no core response is asserted until at least one core request has reached
  the response stage.

## Edge Cases
- `block_size_in_words_p == 1` (single-word lines): block-offset is a tied-zero
  constant.
- `ways_p == 1` (direct-mapped): tag-hit logic is a single comparator; LRU bookkeeping
  is a no-op.
- `sets_p == 1` (single-set): set-index field has zero meaningful bits; the tag field
  absorbs them.
- Atomic on miss: the atomic must produce the correct memory-ordering guarantee even
  though the request stalled for a fill.

## Out of Scope
- Memory-system ordering across multiple `bsg_cache` instances.
- DMA arbiter behaviour beyond "issues fill / eviction packets in request order."
