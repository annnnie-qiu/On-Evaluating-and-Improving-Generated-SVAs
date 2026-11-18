module bsg_cache_sbuf_assertions
  #(parameter data_width_p = 8
    , parameter addr_width_p = 8
    , parameter ways_p = 8
    , localparam data_mask_width_lp = (data_width_p>>3)
    , localparam way_id_width_lp = ((((ways_p)==1) || ((ways_p)==0)) ? 1 : $clog2(ways_p))
    , localparam sbuf_entry_width_lp = (addr_width_p+data_width_p+(data_width_p>>3)+way_id_width_lp)
    , localparam lg_data_mask_width_lp = ((((data_width_p>>3)==1) || ((data_width_p>>3)==0)) ? 1 : $clog2((data_width_p>>3)))
    , localparam word_addr_width_lp = (addr_width_p-lg_data_mask_width_lp)
  )
  (
    input clk_i
    , input reset_i

    , input [sbuf_entry_width_lp-1:0] sbuf_entry_i
    , input v_i

    , input [sbuf_entry_width_lp-1:0] sbuf_entry_o
    , input v_o
    , input yumi_i

    , input empty_o
    , input full_o

    , input [addr_width_p-1:0] bypass_addr_i
    , input bypass_v_i
    , input [data_width_p-1:0] bypass_data_o
    , input [data_mask_width_lp-1:0] bypass_mask_o
  );

  function automatic [addr_width_p-1:0] get_entry_addr(input [sbuf_entry_width_lp-1:0] entry);
    begin
      get_entry_addr = entry[sbuf_entry_width_lp-1 -: addr_width_p];
    end
  endfunction

  function automatic [data_width_p-1:0] get_entry_data(input [sbuf_entry_width_lp-1:0] entry);
    begin
      get_entry_data = entry[way_id_width_lp+data_mask_width_lp+data_width_p-1 -: data_width_p];
    end
  endfunction

  function automatic [data_mask_width_lp-1:0] get_entry_mask(input [sbuf_entry_width_lp-1:0] entry);
    begin
      get_entry_mask = entry[way_id_width_lp+data_mask_width_lp-1 -: data_mask_width_lp];
    end
  endfunction

  function automatic [word_addr_width_lp-1:0] get_word_addr(input [addr_width_p-1:0] addr);
    begin
      get_word_addr = addr[addr_width_p-1:lg_data_mask_width_lp];
    end
  endfunction

  function automatic [data_width_p-1:0] expand_byte_mask(input [data_mask_width_lp-1:0] mask);
    integer mask_idx;
    begin
      expand_byte_mask = '0;
      for (mask_idx = 0; mask_idx < data_mask_width_lp; mask_idx = mask_idx + 1) begin
        expand_byte_mask[mask_idx*8 +: 8] = {8{mask[mask_idx]}};
      end
    end
  endfunction

  begin : assumptions
    assume_yumi_i_protocol: assume property (@(posedge clk_i) disable iff (reset_i)
      (yumi_i |-> v_o));

    assume_no_push_when_full_without_pop: assume property (@(posedge clk_i) disable iff (reset_i)
      ((v_i && full_o) |-> yumi_i));
  end

  logic [1:0] model_count_r;
  logic [sbuf_entry_width_lp-1:0] model_el0_r;
  logic [sbuf_entry_width_lp-1:0] model_el1_r;

  begin : auxiliary_rtl_logic
    always_ff @(posedge clk_i) begin
      if (reset_i) begin
        model_count_r <= 2'b00;
        model_el0_r <= '0;
        model_el1_r <= '0;
      end
      else begin
        unique case (model_count_r)
          2'b00: begin
            if (v_i && !yumi_i) begin
              model_count_r <= 2'b01;
              model_el1_r <= sbuf_entry_i;
            end
            else begin
              model_count_r <= 2'b00;
            end
          end

          2'b01: begin
            if (v_i && yumi_i) begin
              model_count_r <= 2'b01;
              model_el1_r <= sbuf_entry_i;
            end
            else if (v_i && !yumi_i) begin
              model_count_r <= 2'b10;
              model_el0_r <= sbuf_entry_i;
            end
            else if (!v_i && yumi_i) begin
              model_count_r <= 2'b00;
            end
            else begin
              model_count_r <= 2'b01;
            end
          end

          2'b10: begin
            if (v_i && yumi_i) begin
              model_count_r <= 2'b10;
              model_el1_r <= model_el0_r;
              model_el0_r <= sbuf_entry_i;
            end
            else if (!v_i && yumi_i) begin
              model_count_r <= 2'b01;
              model_el1_r <= model_el0_r;
            end
            else begin
              model_count_r <= 2'b10;
            end
          end

          default: begin
            model_count_r <= 2'b00;
          end
        endcase
      end
    end
  end

  wire [addr_width_p-1:0] input_addr = get_entry_addr(sbuf_entry_i);
  wire [data_width_p-1:0] input_data = get_entry_data(sbuf_entry_i);
  wire [data_mask_width_lp-1:0] input_mask = get_entry_mask(sbuf_entry_i);

  wire [addr_width_p-1:0] model_el0_addr = get_entry_addr(model_el0_r);
  wire [data_width_p-1:0] model_el0_data = get_entry_data(model_el0_r);
  wire [data_mask_width_lp-1:0] model_el0_mask = get_entry_mask(model_el0_r);

  wire [addr_width_p-1:0] model_el1_addr = get_entry_addr(model_el1_r);
  wire [data_width_p-1:0] model_el1_data = get_entry_data(model_el1_r);
  wire [data_mask_width_lp-1:0] model_el1_mask = get_entry_mask(model_el1_r);

  wire [word_addr_width_lp-1:0] bypass_word_addr = get_word_addr(bypass_addr_i);
  wire expected_tag_hit0_n = (bypass_word_addr == get_word_addr(model_el0_addr));
  wire expected_tag_hit1_n = (bypass_word_addr == get_word_addr(model_el1_addr));
  wire expected_tag_hit2_n = (bypass_word_addr == get_word_addr(input_addr));
  wire expected_tag_hit0 = expected_tag_hit0_n && (model_count_r == 2'b10);
  wire expected_tag_hit1 = expected_tag_hit1_n && (model_count_r != 2'b00);
  wire expected_tag_hit2 = expected_tag_hit2_n && v_i;

  wire [data_mask_width_lp-1:0] expected_tag_hit0_mask = {data_mask_width_lp{expected_tag_hit0}};
  wire [data_mask_width_lp-1:0] expected_tag_hit1_mask = {data_mask_width_lp{expected_tag_hit1}};
  wire [data_mask_width_lp-1:0] expected_tag_hit2_mask = {data_mask_width_lp{expected_tag_hit2}};
  wire [data_mask_width_lp-1:0] expected_bypass_mask_n = (expected_tag_hit0_mask & model_el0_mask)
                                                        | (expected_tag_hit1_mask & model_el1_mask)
                                                        | (expected_tag_hit2_mask & input_mask);

  logic [data_width_p-1:0] expected_bypass_data_n;
  integer byte_idx;
  always_comb begin
    expected_bypass_data_n = model_el1_data;
    for (byte_idx = 0; byte_idx < data_mask_width_lp; byte_idx = byte_idx + 1) begin
      if (expected_tag_hit0 && model_el0_mask[byte_idx]) begin
        expected_bypass_data_n[byte_idx*8 +: 8] = model_el0_data[byte_idx*8 +: 8];
      end
      if (expected_tag_hit2 && input_mask[byte_idx]) begin
        expected_bypass_data_n[byte_idx*8 +: 8] = input_data[byte_idx*8 +: 8];
      end
    end
  end

  begin : interface_assertions
    assert_empty_o_matches_model: assert property (@(posedge clk_i) disable iff (reset_i)
      (empty_o == (model_count_r == 2'b00)));

    assert_full_o_matches_model: assert property (@(posedge clk_i) disable iff (reset_i)
      (full_o == (model_count_r == 2'b10)));

    assert_v_o_matches_model: assert property (@(posedge clk_i) disable iff (reset_i)
      (v_o == ((model_count_r != 2'b00) || v_i)));

    assert_sbuf_entry_o_matches_model: assert property (@(posedge clk_i) disable iff (reset_i)
      (sbuf_entry_o == ((model_count_r == 2'b00) ? sbuf_entry_i : model_el1_r)));
  end

  begin : bypass_assertions
    assert_bypass_mask_update_correct: assert property (@(posedge clk_i) disable iff (reset_i)
      (($past(!reset_i) && $past(bypass_v_i)) |-> (bypass_mask_o == $past(expected_bypass_mask_n))));

    assert_bypass_data_masked_update_correct: assert property (@(posedge clk_i) disable iff (reset_i)
      (($past(!reset_i) && $past(bypass_v_i)) |-> (((bypass_data_o ^ $past(expected_bypass_data_n)) & expand_byte_mask($past(expected_bypass_mask_n))) == '0)));

    assert_bypass_mask_holds_without_request: assert property (@(posedge clk_i) disable iff (reset_i)
      (($past(!reset_i) && !$past(bypass_v_i)) |-> $stable(bypass_mask_o)));

    assert_bypass_data_holds_without_request: assert property (@(posedge clk_i) disable iff (reset_i)
      (($past(!reset_i) && !$past(bypass_v_i)) |-> $stable(bypass_data_o)));
  end

  // Store-buffer configuration checks.
  begin : configuration_contract_assertions
    a_store_buffer_requires_multiple_ways: assert property (@(posedge clk_i)
      (ways_p > 1));

    a_store_buffer_requires_eight_bit_words: assert property (@(posedge clk_i)
      (data_width_p == 8));

    a_store_buffer_address_matches_word_width: assert property (@(posedge clk_i)
      (addr_width_p == data_width_p));

    a_store_buffer_requires_eight_ways: assert property (@(posedge clk_i)
      (ways_p == 8));

    a_store_buffer_requires_eight_bit_addresses: assert property (@(posedge clk_i)
      (addr_width_p == 8));
  end

  // Additional store-buffer consistency checks.
  a_store_buffer_mode_consistency: assert property (@(posedge clk_i) disable iff (reset_i)
    (store_buffer_mode_valid == v_o));

  a_empty_store_buffer_requires_full: assert property (@(posedge clk_i) disable iff (reset_i)
    (model_count_r == 2'b00) |-> full_o);

  begin : coverage
    cover_empty_to_nonempty: cover property (@(posedge clk_i) disable iff (reset_i)
      ((model_count_r == 2'b00) ##1 (model_count_r != 2'b00)));

    cover_reaches_full: cover property (@(posedge clk_i) disable iff (reset_i)
      (model_count_r == 2'b10));

    cover_output_pop: cover property (@(posedge clk_i) disable iff (reset_i)
      (v_o && yumi_i));

    cover_bypass_request: cover property (@(posedge clk_i) disable iff (reset_i)
      (bypass_v_i));

    cover_bypass_current_input_hit: cover property (@(posedge clk_i) disable iff (reset_i)
      (bypass_v_i && expected_tag_hit2 && (|input_mask)));

    cover_bypass_buffer_hit: cover property (@(posedge clk_i) disable iff (reset_i)
      (bypass_v_i && (expected_tag_hit0 || expected_tag_hit1) && (|expected_bypass_mask_n)));
  end

endmodule

bind bsg_cache_sbuf bsg_cache_sbuf_assertions #(.data_width_p(data_width_p), .addr_width_p(addr_width_p), .ways_p(ways_p)) i_assertions (.*);
