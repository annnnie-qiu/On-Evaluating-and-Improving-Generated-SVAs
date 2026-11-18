bind bsg_cache_tbuf
bsg_cache_tbuf_assertions
bsg_cache_tbuf_assertions_inst
(
    .clk_i(clk_i),
    .reset_i(reset_i),

    .addr_i(addr_i),
    .way_i(way_i),

    .v_i(v_i),

    .addr_o(addr_o),
    .way_o(way_o),

    .v_o(v_o),
    .yumi_i(yumi_i),

    .empty_o(empty_o),
    .full_o(full_o),

    .bypass_addr_i(bypass_addr_i),
    .bypass_v_i(bypass_v_i),
    .bypass_track_o(bypass_track_o)
);