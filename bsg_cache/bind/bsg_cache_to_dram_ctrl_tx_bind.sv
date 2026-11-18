bind bsg_cache_to_dram_ctrl_tx
bsg_cache_to_dram_ctrl_tx_assertions
bsg_cache_to_dram_ctrl_tx_assertions_inst
(
    .clk_i(clk_i),
    .reset_i(reset_i),

    .dma_mask_i(dma_mask_i),
    .dma_data_i(dma_data_i),
    .dma_data_v_i(dma_data_v_i),

    .dma_data_yumi_o(dma_data_yumi_o),

    .app_wdf_wren_o(app_wdf_wren_o),
    .app_wdf_data_o(app_wdf_data_o),
    .app_wdf_mask_o(app_wdf_mask_o),
    .app_wdf_end_o(app_wdf_end_o),

    .app_wdf_rdy_i(app_wdf_rdy_i)
);