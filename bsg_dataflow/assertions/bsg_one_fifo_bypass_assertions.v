module bsg_one_fifo_bypass_assertions #(
  parameter width_p = 8
) (
  input                  clk_i,
  input                  reset_i,
  input                  ready_and_o,
  input [width_p-1:0]    data_i,
  input                  v_i,
  input                  v_o,
  input [width_p-1:0]    data_o,
  input                  yumi_i
);

  generate
    begin : combinational_interface_properties
      a_valid_ready_relation: assert property (@(posedge clk_i) disable iff (reset_i)
        (v_o == ((~ready_and_o) | v_i))
      );

      a_empty_bypass_data: assert property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o |-> (data_o == data_i))
      );

      a_empty_valid_equals_input_valid: assert property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o |-> (v_o == v_i))
      );

      a_full_valid_asserted: assert property (@(posedge clk_i) disable iff (reset_i)
        ((!ready_and_o) |-> v_o)
      );
    end

    begin : sequential_state_properties
      a_ready_updates_from_previous_output: assert property (@(posedge clk_i) disable iff (reset_i)
        (1'b1 |=> (ready_and_o == ~(($past(v_o)) & ~($past(yumi_i)))))
      );

      a_empty_no_valid_remains_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        ((ready_and_o && !v_i) |=> ready_and_o)
      );

      a_empty_bypass_transfer_remains_empty: assert property (@(posedge clk_i) disable iff (reset_i)
        ((ready_and_o && v_i && yumi_i) |=> ready_and_o)
      );

      a_empty_unc onsumed_valid_becomes_full: assert property (@(posedge clk_i) disable iff (reset_i)
        ((ready_and_o && v_i && !yumi_i) |=> !ready_and_o)
      );

      a_full_holds_without_yumi: assert property (@(posedge clk_i) disable iff (reset_i)
        ((!ready_and_o && !yumi_i) |=> !ready_and_o)
      );

      a_full_drains_with_yumi: assert property (@(posedge clk_i) disable iff (reset_i)
        ((!ready_and_o && yumi_i) |=> ready_and_o)
      );
    end

    begin : sequential_data_properties
      a_capture_data_next: assert property (@(posedge clk_i) disable iff (reset_i)
        ((ready_and_o && v_i && !yumi_i) |=> (data_o == $past(data_i)))
      );

      a_full_data_stable_without_yumi: assert property (@(posedge clk_i) disable iff (reset_i)
        ((!ready_and_o && !yumi_i) |=> (data_o == $past(data_o)))
      );

      a_full_drain_next_bypass_data: assert property (@(posedge clk_i) disable iff (reset_i)
        ((!ready_and_o && yumi_i) |=> (ready_and_o && (data_o == data_i)))
      );

      a_empty_no_valid_next_bypass_data: assert property (@(posedge clk_i) disable iff (reset_i)
        ((ready_and_o && !v_i) |=> (ready_and_o && (data_o == data_i)))
      );
    end

    begin : cover_properties
      c_empty_state_observed: cover property (@(posedge clk_i) disable iff (reset_i)
        ready_and_o
      );

      c_full_state_observed: cover property (@(posedge clk_i) disable iff (reset_i)
        !ready_and_o
      );

      c_bypass_transfer: cover property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o && v_i && yumi_i && v_o)
      );

      c_capture_to_full: cover property (@(posedge clk_i) disable iff (reset_i)
        ((ready_and_o && v_i && !yumi_i) ##1 !ready_and_o)
      );

      c_full_hold_cycle: cover property (@(posedge clk_i) disable iff (reset_i)
        ((!ready_and_o && !yumi_i) ##1 !ready_and_o)
      );

      c_full_drain_to_empty: cover property (@(posedge clk_i) disable iff (reset_i)
        ((!ready_and_o && yumi_i) ##1 ready_and_o)
      );

      c_empty_idle_cycle: cover property (@(posedge clk_i) disable iff (reset_i)
        (ready_and_o && !v_i)
      );
    end
  endgenerate

endmodule

bind bsg_one_fifo_bypass bsg_one_fifo_bypass_assertions #(.width_p(width_p)) i_assertions (.*);