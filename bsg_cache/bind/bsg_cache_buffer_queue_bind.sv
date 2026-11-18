bind bsg_cache_buffer_queue
bsg_cache_buffer_queue_assertions
#(
    .width_p(width_p)
)
bsg_cache_buffer_queue_assertions_inst
(
    .clk_i(clk_i),
    .reset_i(reset_i),

    .v_i(v_i),
    .data_i(data_i),

    .v_o(v_o),
    .data_o(data_o),

    .yumi_i(yumi_i),

    .el0_valid_o(el0_valid_o),
    .el1_valid_o(el1_valid_o),

    .el0_snoop_o(el0_snoop_o),
    .el1_snoop_o(el1_snoop_o),

    .empty_o(empty_o),
    .full_o(full_o)
);