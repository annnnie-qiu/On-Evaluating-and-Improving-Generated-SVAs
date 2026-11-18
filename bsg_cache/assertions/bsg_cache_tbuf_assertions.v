module bsg_cache_tbuf_assertions
  #(parameter data_width_p = 8
    ,parameter addr_width_p = 8
    ,parameter ways_p = 8
    ,localparam way_id_width_lp = ((((ways_p)==1) || ((ways_p)==0)) ? 1 : $clog2((ways_p)))
  )
  (
    input clk_i
    ,input reset_i

    ,input [addr_width_p-1:0] addr_i
    ,input [way_id_width_lp-1:0] way_i
    ,input v_i

    ,input [addr_width_p-1:0] addr_o
    ,input [way_id_width_lp-1:0] way_o
    ,input v_o
    ,input yumi_i

    ,input empty_o
    ,input full_o

    ,input [addr_width_p-1:0] bypass_addr_i
    ,input bypass_v_i
    ,input bypass_track_o
  );

  localparam lg_data_mask_width_lp = ((((data_width_p>>3)==1) || ((data_width_p>>3)==0)) ? 1 : $clog2((data_width_p>>3)));
  localparam word_addr_width_lp = addr_width_p-lg_data_mask_width_lp;

  // auxiliary_rtl_logic
  logic past_valid_r;
  logic [1:0] ghost_count_r;
  logic [addr_width_p-1:0] ghost_head_addr_r;
  logic [way_id_width_lp-1:0] ghost_head_way_r;
  logic [addr_width_p-1:0] ghost_tail_addr_r;
  logic [way_id_width_lp-1:0] ghost_tail_way_r;

  wire output_fire = v_o & yumi_i;

  always_ff @(posedge clk_i) begin
    if (reset_i) begin
      past_valid_r <= 1'b0;
      ghost_count_r <= 2'd0;
      ghost_head_addr_r <= '0;
      ghost_head_way_r <= '0;
      ghost_tail_addr_r <= '0;
      ghost_tail_way_r <= '0;
    end
    else begin
      past_valid_r <= 1'b1;
      case (ghost_count_r)
        2'd0: begin
          if (v_i && !output_fire) begin
            ghost_count_r <= 2'd1;
            ghost_head_addr_r <= addr_i;
            ghost_head_way_r <= way_i;
          end
          else begin
            ghost_count_r <= 2'd0;
          end
        end
        2'd1: begin
          if (output_fire) begin
            if (v_i) begin
              ghost_count_r <= 2'd1;
              ghost_head_addr_r <= addr_i;
              ghost_head_way_r <= way_i;
            end
            else begin
              ghost_count_r <= 2'd0;
            end
          end
          else begin
            if (v_i) begin
              ghost_count_r <= 2'd2;
              ghost_tail_addr_r <= addr_i;
              ghost_tail_way_r <= way_i;
            end
            else begin
              ghost_count_r <= 2'd1;
            end
          end
        end
        2'd2: begin
          if (output_fire) begin
            if (v_i) begin
              ghost_count_r <= 2'd2;
              ghost_head_addr_r <= ghost_tail_addr_r;
              ghost_head_way_r <= ghost_tail_way_r;
              ghost_tail_addr_r <= addr_i;
              ghost_tail_way_r <= way_i;
            end
            else begin
              ghost_count_r <= 2'd1;
              ghost_head_addr_r <= ghost_tail_addr_r;
              ghost_head_way_r <= ghost_tail_way_r;
            end
          end
          else begin
            ghost_count_r <= 2'd2;
          end
        end
        default: begin
          ghost_count_r <= 2'd0;
        end
      endcase
    end
  end

  wire [word_addr_width_lp-1:0] bypass_word_addr = bypass_addr_i[addr_width_p-1:lg_data_mask_width_lp];
  wire [word_addr_width_lp-1:0] input_word_addr = addr_i[addr_width_p-1:lg_data_mask_width_lp];
  wire [word_addr_width_lp-1:0] ghost_head_word_addr = ghost_head_addr_r[addr_width_p-1:lg_data_mask_width_lp];
  wire [word_addr_width_lp-1:0] ghost_tail_word_addr = ghost_tail_addr_r[addr_width_p-1:lg_data_mask_width_lp];

  wire bypass_hit_head_expected = (ghost_count_r != 2'd0) && (bypass_word_addr == ghost_head_word_addr);
  wire bypass_hit_tail_expected = (ghost_count_r == 2'd2) && (bypass_word_addr == ghost_tail_word_addr);
  wire bypass_hit_input_expected = v_i && (bypass_word_addr == input_word_addr);
  wire bypass_hit_expected = bypass_hit_head_expected | bypass_hit_tail_expected | bypass_hit_input_expected;

  generate
    if (1) begin : protocol_assumptions
      assume_yumi_only_when_model_valid: assume property (@(posedge clk_i) disable iff (reset_i)
        (yumi_i |-> ((ghost_count_r != 2'd0) || v_i)));

      assume_no_push_when_model_full_without_yumi: assume property (@(posedge clk_i) disable iff (reset_i)
        ((v_i && (ghost_count_r == 2'd2)) |-> yumi_i));
    end
  endgenerate

  generate
    if (1) begin : queue_interface_assertions
      assert_ghost_count_bounds: assert property (@(posedge clk_i) disable iff (reset_i)
        (ghost_count_r inside {2'd0, 2'd1, 2'd2}));

      assert_empty_matches_ghost_count: assert property (@(posedge clk_i) disable iff (reset_i)
        (empty_o == (ghost_count_r == 2'd0)));

      assert_full_matches_ghost_count: assert property (@(posedge clk_i) disable iff (reset_i)
        (full_o == (ghost_count_r == 2'd2)));

      assert_empty_full_mutual_exclusion: assert property (@(posedge clk_i) disable iff (reset_i)
        (!(empty_o && full_o)));

      assert_valid_matches_ghost_or_input: assert property (@(posedge clk_i) disable iff (reset_i)
        (v_o == ((ghost_count_r != 2'd0) || v_i)));

      assert_output_data_when_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        ((ghost_count_r == 2'd0) |-> ({addr_o, way_o} == {addr_i, way_i})));

      assert_output_data_when_nonempty: assert property (@(posedge clk_i) disable iff (reset_i)
        ((ghost_count_r != 2'd0) |-> ({addr_o, way_o} == {ghost_head_addr_r, ghost_head_way_r})));

      assert_output_fire_data_matches_head_or_input: assert property (@(posedge clk_i) disable iff (reset_i)
        ((v_o && yumi_i) |-> ({addr_o, way_o} == ((ghost_count_r == 2'd0) ? {addr_i, way_i} : {ghost_head_addr_r, ghost_head_way_r}))));
    end
  endgenerate

  generate
    if (1) begin : bypass_assertions
      assert_bypass_capture_when_valid: assert property (@(posedge clk_i) disable iff (reset_i)
        ((past_valid_r && $past(!reset_i) && $past(bypass_v_i)) |-> (bypass_track_o == $past(bypass_hit_expected))));

      assert_bypass_hold_when_not_valid: assert property (@(posedge clk_i) disable iff (reset_i)
        ((past_valid_r && $past(!reset_i) && !$past(bypass_v_i)) |-> $stable(bypass_track_o)));
    end
  endgenerate

  generate
    if (1) begin : coverage_properties
      cover_accept_input: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_i && !full_o));

      cover_output_fire: cover property (@(posedge clk_i) disable iff (reset_i)
        (v_o && yumi_i));

      cover_reaches_full: cover property (@(posedge clk_i) disable iff (reset_i)
        (!full_o ##1 full_o));

      cover_reaches_empty: cover property (@(posedge clk_i) disable iff (reset_i)
        (!empty_o ##1 empty_o));

      cover_bypass_hit_capture: cover property (@(posedge clk_i) disable iff (reset_i)
        (bypass_v_i && bypass_hit_expected ##1 bypass_track_o));

      cover_bypass_miss_capture: cover property (@(posedge clk_i) disable iff (reset_i)
        (bypass_v_i && !bypass_hit_expected ##1 !bypass_track_o));

      cover_bypass_hit_from_tail: cover property (@(posedge clk_i) disable iff (reset_i)
        (bypass_v_i && bypass_hit_tail_expected ##1 bypass_track_o));

      cover_bypass_hit_from_head: cover property (@(posedge clk_i) disable iff (reset_i)
        (bypass_v_i && bypass_hit_head_expected ##1 bypass_track_o));

      cover_bypass_hit_from_input: cover property (@(posedge clk_i) disable iff (reset_i)
        (bypass_v_i && bypass_hit_input_expected ##1 bypass_track_o));
    end
  endgenerate

endmodule

bind bsg_cache_tbuf bsg_cache_tbuf_assertions #(.data_width_p(data_width_p), .addr_width_p(addr_width_p), .ways_p(ways_p)) i_assertions (.*);