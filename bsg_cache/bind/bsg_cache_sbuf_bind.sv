bind bsg_cache_sbuf
bsg_cache_sbuf_assertions
bsg_cache_sbuf_assertions_inst
(
    .clk_i(clk_i),
    .reset_i(reset_i),

    .sbuf_entry_i(sbuf_entry_i),
    .v_i(v_i),

    .sbuf_entry_o(sbuf_entry_o),
    .v_o(v_o),

    .yumi_i(yumi_i),

    .empty_o(empty_o),
    .full_o(full_o),

    .bypass_addr_i(bypass_addr_i),
    .bypass_v_i(bypass_v_i),

    .bypass_data_o(bypass_data_o),
    .bypass_mask_o(bypass_mask_o)
);