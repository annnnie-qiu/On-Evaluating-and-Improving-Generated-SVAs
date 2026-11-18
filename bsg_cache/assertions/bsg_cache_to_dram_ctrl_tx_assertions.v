module bsg_cache_to_dram_ctrl_tx_assertions
  #(parameter num_dma_p = 8
    , parameter dma_data_width_p = 8
    , parameter dma_burst_len_p = 8
    , parameter dram_ctrl_burst_len_p = 8
    , parameter dma_mask_width_p = 8

    , localparam mask_width_lp=(dma_data_width_p>>3)
    , localparam dma_byte_mask_width_lp=(dma_burst_len_p*mask_width_lp)
    , localparam num_req_lp=(dma_burst_len_p/dram_ctrl_burst_len_p)
    , localparam lg_num_dma_lp=( (((num_dma_p)==1) || ((num_dma_p)==0))? 1 : $clog2((num_dma_p)))
    , localparam lg_dram_ctrl_burst_len_lp=( (((dram_ctrl_burst_len_p)==1) || ((dram_ctrl_burst_len_p)==0))? 1 : $clog2((dram_ctrl_burst_len_p)))
    , localparam expand_factor_lp=(dma_byte_mask_width_lp/dma_mask_width_p)
  )
  (
    input clk_i
    , input reset_i

    , input [dma_mask_width_p-1:0] dma_mask_i
    , input [dma_data_width_p-1:0] dma_data_i
    , input dma_data_v_i
    , input dma_data_yumi_o

    , input app_wdf_wren_o
    , input [dma_data_width_p-1:0] app_wdf_data_o
    , input [mask_width_lp-1:0] app_wdf_mask_o
    , input app_wdf_end_o
    , input app_wdf_rdy_i
  );

  localparam [lg_dram_ctrl_burst_len_lp-1:0] dram_last_count_lp = (dram_ctrl_burst_len_p-1);

  logic [lg_dram_ctrl_burst_len_lp-1:0] shadow_count_r;

  wire take_word_expected = dma_data_v_i & app_wdf_rdy_i;
  wire shadow_at_last = (shadow_count_r == dram_last_count_lp);

  function automatic [dma_byte_mask_width_lp-1:0] expand_dma_mask(input [dma_mask_width_p-1:0] mask_i);
    integer idx;
    integer rep;
    begin
      expand_dma_mask = '0;
      for (idx = 0; idx < dma_mask_width_p; idx = idx + 1) begin
        for (rep = 0; rep < expand_factor_lp; rep = rep + 1) begin
          expand_dma_mask[(idx*expand_factor_lp)+rep] = mask_i[idx];
        end
      end
    end
  endfunction

  function automatic [mask_width_lp-1:0] expected_wdf_mask(input [dma_mask_width_p-1:0] mask_i, input [lg_dram_ctrl_burst_len_lp-1:0] count_i);
    logic [dma_byte_mask_width_lp-1:0] expanded_mask;
    integer bit_idx;
    integer src_idx;
    begin
      expanded_mask = expand_dma_mask(mask_i);
      expected_wdf_mask = '0;
      for (bit_idx = 0; bit_idx < mask_width_lp; bit_idx = bit_idx + 1) begin
        src_idx = (int'(count_i) * mask_width_lp) + bit_idx;
        if (src_idx < dma_byte_mask_width_lp) begin
          expected_wdf_mask[bit_idx] = ~expanded_mask[src_idx];
        end
      end
    end
  endfunction

  wire [mask_width_lp-1:0] expected_mask = expected_wdf_mask(dma_mask_i, shadow_count_r);

  generate
    begin : auxiliary_rtl_logic
      always_ff @(posedge clk_i) begin
        if (reset_i) begin
          shadow_count_r <= '0;
        end
        else if (take_word_expected) begin
          if (shadow_at_last) begin
            shadow_count_r <= '0;
          end
          else begin
            shadow_count_r <= shadow_count_r + 1'b1;
          end
        end
      end
    end

    begin : input_protocol_assumptions
      as_dma_inputs_stable_while_stalled: assume property (@(posedge clk_i) disable iff (reset_i)
        (dma_data_v_i && !app_wdf_rdy_i) |=> (dma_data_v_i && $stable(dma_data_i) && $stable(dma_mask_i))
      );
    end

    begin : direct_output_assertions
      a_dma_data_yumi_matches_valid_ready: assert property (@(posedge clk_i) disable iff (reset_i)
        dma_data_yumi_o == (dma_data_v_i & app_wdf_rdy_i)
      );

      a_app_wdf_wren_matches_dma_valid: assert property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_wren_o == dma_data_v_i
      );

      a_dma_yumi_matches_wdf_wren_ready: assert property (@(posedge clk_i) disable iff (reset_i)
        dma_data_yumi_o == (app_wdf_wren_o & app_wdf_rdy_i)
      );

      a_app_wdf_data_passes_dma_data: assert property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_data_o == dma_data_i
      );
    end

    begin : burst_end_assertions
      a_app_wdf_end_matches_last_accepted_word: assert property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_end_o == (take_word_expected && shadow_at_last)
      );

      a_app_wdf_end_implies_dma_yumi: assert property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_end_o |-> dma_data_yumi_o
      );

      a_no_app_wdf_end_without_take_word: assert property (@(posedge clk_i) disable iff (reset_i)
        !take_word_expected |-> !app_wdf_end_o
      );

      a_take_before_last_does_not_end: assert property (@(posedge clk_i) disable iff (reset_i)
        (take_word_expected && !shadow_at_last) |-> !app_wdf_end_o
      );

      a_last_take_asserts_end: assert property (@(posedge clk_i) disable iff (reset_i)
        (take_word_expected && shadow_at_last) |-> app_wdf_end_o
      );
    end

    begin : mask_output_assertions
      a_app_wdf_mask_matches_selected_dma_mask: assert property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_mask_o == expected_mask
      );

      a_all_zero_dma_mask_drives_all_one_wdf_mask: assert property (@(posedge clk_i) disable iff (reset_i)
        (dma_mask_i == '0) |-> (app_wdf_mask_o == '1)
      );

    end
    

    begin : controlled_syntax_failure
      a_controlled_syntax_fail: assert property (
        @(posedge clk_i) disable iff (reset_i)
        (app_wdf_data_o == dma_data_i) && && app_wdf_wren_o
      );
    end

    begin : interface_covers
      c_dma_handshake_fire: cover property (@(posedge clk_i) disable iff (reset_i)
        dma_data_v_i && app_wdf_rdy_i && dma_data_yumi_o && app_wdf_wren_o
      );

      c_dma_stalled_valid: cover property (@(posedge clk_i) disable iff (reset_i)
        dma_data_v_i && !app_wdf_rdy_i && !dma_data_yumi_o && app_wdf_wren_o
      );

      c_app_wdf_end_fire: cover property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_end_o && dma_data_yumi_o
      );

      c_nonending_word_accepted: cover property (@(posedge clk_i) disable iff (reset_i)
        take_word_expected && !app_wdf_end_o
      );

      c_all_zero_wdf_mask_observed: cover property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_mask_o == '0
      );

      c_all_one_wdf_mask_observed: cover property (@(posedge clk_i) disable iff (reset_i)
        app_wdf_mask_o == '1
      );

      c_nonzero_data_write_observed: cover property (@(posedge clk_i) disable iff (reset_i)
        dma_data_yumi_o && (app_wdf_data_o != '0)
      );
    end
  endgenerate

endmodule

bind bsg_cache_to_dram_ctrl_tx bsg_cache_to_dram_ctrl_tx_assertions #(.num_dma_p(num_dma_p), .dma_data_width_p(dma_data_width_p), .dma_burst_len_p(dma_burst_len_p), .dram_ctrl_burst_len_p(dram_ctrl_burst_len_p), .dma_mask_width_p(dma_mask_width_p)) i_assertions (.*);
