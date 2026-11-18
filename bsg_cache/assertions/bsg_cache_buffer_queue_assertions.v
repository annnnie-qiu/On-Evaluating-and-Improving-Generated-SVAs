module bsg_cache_buffer_queue_assertions
  #(parameter width_p = 8)
  (
    input clk_i,
    input reset_i,

    input v_i,
    input [width_p-1:0] data_i,

    input v_o,
    input [width_p-1:0] data_o,
    input yumi_i,

    input el0_valid_o,
    input el1_valid_o,
    input [width_p-1:0] el0_snoop_o,
    input [width_p-1:0] el1_snoop_o,

    input empty_o,
    input full_o
  );

  logic [1:0] q_count_r;
  logic [width_p-1:0] q_head_r;
  logic [width_p-1:0] q_tail_r;

  generate
    if (1) begin : auxiliary_rtl_logic
      always_ff @(posedge clk_i) begin
        if (reset_i) begin
          q_count_r <= 2'd0;
        end
        else begin
          unique case (q_count_r)
            2'd0: begin
              if (v_i && !yumi_i) begin
                q_count_r <= 2'd1;
                q_head_r <= data_i;
              end
              else begin
                q_count_r <= 2'd0;
              end
            end

            2'd1: begin
              if (yumi_i) begin
                if (v_i) begin
                  q_count_r <= 2'd1;
                  q_head_r <= data_i;
                end
                else begin
                  q_count_r <= 2'd0;
                end
              end
              else begin
                if (v_i) begin
                  q_count_r <= 2'd2;
                  q_tail_r <= data_i;
                end
                else begin
                  q_count_r <= 2'd1;
                end
              end
            end

            2'd2: begin
              if (yumi_i) begin
                q_head_r <= q_tail_r;
                if (v_i) begin
                  q_count_r <= 2'd2;
                  q_tail_r <= data_i;
                end
                else begin
                  q_count_r <= 2'd1;
                end
              end
              else begin
                q_count_r <= 2'd2;
              end
            end

            default: begin
              q_count_r <= 2'd0;
            end
          endcase
        end
      end
    end : auxiliary_rtl_logic
  endgenerate

  generate
    if (1) begin : protocol_assumptions
      a_no_enqueue_when_model_full_without_dequeue: assume property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd2) |-> (!v_i || yumi_i)
      );

      a_yumi_only_when_model_valid: assume property (@(posedge clk_i) disable iff (reset_i)
        yumi_i |-> ((q_count_r != 2'd0) || v_i)
      );
    end : protocol_assumptions
  endgenerate

  generate
    if (1) begin : combinational_output_assertions
      a_empty_state_outputs: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd0) |->
          (v_o == v_i)
          && (data_o == data_i)
          && empty_o
          && !full_o
          && !el0_valid_o
          && !el1_valid_o
      );

      a_one_element_state_outputs: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1) |->
          v_o
          && !empty_o
          && !full_o
          && !el0_valid_o
          && el1_valid_o
          && (data_o == q_head_r)
          && (el1_snoop_o == q_head_r)
      );

      a_two_element_state_outputs: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd2) |->
          v_o
          && !empty_o
          && full_o
          && el0_valid_o
          && el1_valid_o
          && (data_o == q_head_r)
          && (el1_snoop_o == q_head_r)
          && (el0_snoop_o == q_tail_r)
      );

      a_empty_flag_equivalence: assert property (@(posedge clk_i) disable iff (reset_i)
        empty_o == (q_count_r == 2'd0)
      );

      a_full_flag_equivalence: assert property (@(posedge clk_i) disable iff (reset_i)
        full_o == (q_count_r == 2'd2)
      );

      a_output_valid_equivalence: assert property (@(posedge clk_i) disable iff (reset_i)
        v_o == ((q_count_r != 2'd0) || v_i)
      );

      a_el0_valid_equivalence: assert property (@(posedge clk_i) disable iff (reset_i)
        el0_valid_o == (q_count_r == 2'd2)
      );

      a_el1_valid_equivalence: assert property (@(posedge clk_i) disable iff (reset_i)
        el1_valid_o == (q_count_r != 2'd0)
      );

      a_full_and_empty_mutually_exclusive: assert property (@(posedge clk_i) disable iff (reset_i)
        !(full_o && empty_o)
      );

      a_data_o_selects_head_or_input: assert property (@(posedge clk_i) disable iff (reset_i)
        el1_valid_o ? (data_o == el1_snoop_o) : (data_o == data_i)
      );
    end : combinational_output_assertions
  endgenerate

  generate
    if (1) begin : temporal_queue_assertions
      a_empty_enqueue_captures_head: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd0 && v_i && !yumi_i) |=>
          el1_valid_o
          && !el0_valid_o
          && !empty_o
          && !full_o
          && (el1_snoop_o == $past(data_i))
          && (data_o == $past(data_i))
      );

      a_empty_pass_through_consumed_remains_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd0 && v_i && yumi_i) |=>
          empty_o
          && !full_o
          && !el0_valid_o
          && !el1_valid_o
      );

      a_one_enqueue_without_dequeue_fills_tail: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1 && v_i && !yumi_i) |=>
          full_o
          && el0_valid_o
          && el1_valid_o
          && (el1_snoop_o == $past(q_head_r))
          && (el0_snoop_o == $past(data_i))
          && (data_o == $past(q_head_r))
      );

      a_one_dequeue_without_enqueue_empties: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1 && !v_i && yumi_i) |=>
          empty_o
          && !full_o
          && !el0_valid_o
          && !el1_valid_o
      );

      a_one_dequeue_with_enqueue_replaces_head: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1 && v_i && yumi_i) |=>
          !empty_o
          && !full_o
          && !el0_valid_o
          && el1_valid_o
          && (el1_snoop_o == $past(data_i))
          && (data_o == $past(data_i))
      );

      a_two_dequeue_without_enqueue_promotes_tail: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd2 && !v_i && yumi_i) |=>
          !empty_o
          && !full_o
          && !el0_valid_o
          && el1_valid_o
          && (el1_snoop_o == $past(q_tail_r))
          && (data_o == $past(q_tail_r))
      );

      a_two_dequeue_with_enqueue_keeps_full: assert property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd2 && v_i && yumi_i) |=>
          !empty_o
          && full_o
          && el0_valid_o
          && el1_valid_o
          && (el1_snoop_o == $past(q_tail_r))
          && (el0_snoop_o == $past(data_i))
          && (data_o == $past(q_tail_r))
      );
    end : temporal_queue_assertions
  endgenerate

  generate
    if (1) begin : coverage_properties
      c_empty_pass_through_fire: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd0 && v_i && yumi_i)
      );

      c_empty_enqueue_to_one: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd0 && v_i && !yumi_i)
      );

      c_one_element_hold: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1 && !v_i && !yumi_i)
      );

      c_one_enqueue_to_full: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1 && v_i && !yumi_i)
      );

      c_one_dequeue_to_empty: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1 && !v_i && yumi_i)
      );

      c_one_replace_head: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd1 && v_i && yumi_i)
      );

      c_full_hold_no_enqueue: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd2 && !v_i && !yumi_i)
      );

      c_full_dequeue_to_one: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd2 && !v_i && yumi_i)
      );

      c_full_dequeue_enqueue_stays_full: cover property (@(posedge clk_i) disable iff (reset_i)
        (q_count_r == 2'd2 && v_i && yumi_i)
      );
    end : coverage_properties
  endgenerate

endmodule

bind bsg_cache_buffer_queue bsg_cache_buffer_queue_assertions #(.width_p(width_p)) i_assertions (.*);