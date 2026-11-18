module bsg_flow_convert_assertions
  #(parameter send_v_and_ready_p    = 0
    , parameter send_v_then_yumi_p  = 0
    , parameter send_ready_then_v_p = 0
    , parameter send_retry_then_v_p = 0
    , parameter send_v_and_retry_p  = 0
    , parameter recv_v_and_ready_p  = 0
    , parameter recv_v_then_yumi_p  = 0
    , parameter recv_ready_then_v_p = 0
    , parameter recv_v_and_retry_p  = 0
    , parameter recv_v_then_retry_p = 0
    , parameter width_p = 1
    )
   (input [width_p-1:0] v_i
    , input [width_p-1:0] fc_o
    , input [width_p-1:0] v_o
    , input [width_p-1:0] fc_i
    );

   generate
      if (((send_v_then_yumi_p != 0) && (recv_v_and_ready_p != 0))
          || ((send_v_then_yumi_p != 0) && (recv_ready_then_v_p != 0))) begin : gen_fc_o_yumi_ready
         always_comb begin : fc_o_yumi_ready_checks
            a_fc_o_yumi_ready_exact: assert (fc_o == (fc_i & v_i));
            a_fc_o_yumi_ready_no_extra_valid: assert ((fc_o & ~v_i) == {width_p{1'b0}});
            a_fc_o_yumi_ready_no_extra_flow_control: assert ((fc_o & ~fc_i) == {width_p{1'b0}});
            c_fc_o_yumi_ready_all_zero: cover (fc_o == {width_p{1'b0}});
            c_fc_o_yumi_ready_all_one: cover (fc_o == {width_p{1'b1}});
            c_fc_o_yumi_ready_masking_active: cover ((fc_i != v_i) && (fc_o == (fc_i & v_i)));
         end
      end
      else if ((send_v_then_yumi_p != 0) && (recv_v_and_retry_p != 0)) begin : gen_fc_o_yumi_retry
         always_comb begin : fc_o_yumi_retry_checks
            a_fc_o_yumi_retry_exact: assert (fc_o == (~fc_i & v_i));
            a_fc_o_yumi_retry_no_extra_valid: assert ((fc_o & ~v_i) == {width_p{1'b0}});
            a_fc_o_yumi_retry_blocks_retry: assert ((fc_o & fc_i) == {width_p{1'b0}});
            c_fc_o_yumi_retry_all_zero: cover (fc_o == {width_p{1'b0}});
            c_fc_o_yumi_retry_all_one: cover (fc_o == {width_p{1'b1}});
            c_fc_o_yumi_retry_inversion_masking_active: cover ((fc_i != {width_p{1'b0}}) && (v_i != {width_p{1'b0}}) && (fc_o == (~fc_i & v_i)));
         end
      end
      else if ((send_ready_then_v_p != 0) && (recv_v_then_yumi_p != 0)) begin : gen_fc_o_unhandled_ready_yumi
      end
      else if ((send_ready_then_v_p != 0) && (recv_v_then_retry_p != 0)) begin : gen_fc_o_unhandled_ready_retry
      end
      else if ((send_retry_then_v_p != 0) && (recv_v_then_yumi_p != 0)) begin : gen_fc_o_unhandled_retry_yumi
      end
      else if (((send_retry_then_v_p != 0) || (send_v_and_retry_p != 0))
               != ((recv_v_then_retry_p != 0) || (recv_v_and_retry_p != 0))) begin : gen_fc_o_invert
         always_comb begin : fc_o_invert_checks
            a_fc_o_invert_exact: assert (fc_o == ~fc_i);
            a_fc_o_invert_complement: assert ((fc_o ^ fc_i) == {width_p{1'b1}});
            c_fc_o_invert_all_zero: cover (fc_o == {width_p{1'b0}});
            c_fc_o_invert_all_one: cover (fc_o == {width_p{1'b1}});
            c_fc_o_invert_changes_value: cover (fc_o != fc_i);
         end
      end
      else begin : gen_fc_o_passthrough
         always_comb begin : fc_o_passthrough_checks
            a_fc_o_passthrough_exact: assert (fc_o == fc_i);
            a_fc_o_passthrough_no_difference: assert ((fc_o ^ fc_i) == {width_p{1'b0}});
            c_fc_o_passthrough_all_zero: cover (fc_o == {width_p{1'b0}});
            c_fc_o_passthrough_all_one: cover (fc_o == {width_p{1'b1}});
            c_fc_o_passthrough_nonzero: cover (fc_o != {width_p{1'b0}});
         end
      end
   endgenerate

   generate
      if ((recv_ready_then_v_p != 0) && (send_ready_then_v_p == 0)) begin : gen_v_o_ready_gate
         always_comb begin : v_o_ready_gate_checks
            a_v_o_ready_gate_exact: assert (v_o == (v_i & fc_i));
            a_v_o_ready_gate_no_extra_valid: assert ((v_o & ~v_i) == {width_p{1'b0}});
            a_v_o_ready_gate_no_extra_flow_control: assert ((v_o & ~fc_i) == {width_p{1'b0}});
            c_v_o_ready_gate_all_zero: cover (v_o == {width_p{1'b0}});
            c_v_o_ready_gate_all_one: cover (v_o == {width_p{1'b1}});
            c_v_o_ready_gate_masking_active: cover ((v_i != {width_p{1'b0}}) && (fc_i == {width_p{1'b0}}) && (v_o == {width_p{1'b0}}));
         end
      end
      else begin : gen_v_o_passthrough
         always_comb begin : v_o_passthrough_checks
            a_v_o_passthrough_exact: assert (v_o == v_i);
            a_v_o_passthrough_no_difference: assert ((v_o ^ v_i) == {width_p{1'b0}});
            c_v_o_passthrough_all_zero: cover (v_o == {width_p{1'b0}});
            c_v_o_passthrough_all_one: cover (v_o == {width_p{1'b1}});
            c_v_o_passthrough_nonzero: cover (v_o != {width_p{1'b0}});
         end
      end
   endgenerate

endmodule

bind bsg_flow_convert bsg_flow_convert_assertions #(.send_v_and_ready_p(send_v_and_ready_p), .send_v_then_yumi_p(send_v_then_yumi_p), .send_ready_then_v_p(send_ready_then_v_p), .send_retry_then_v_p(send_retry_then_v_p), .send_v_and_retry_p(send_v_and_retry_p), .recv_v_and_ready_p(recv_v_and_ready_p), .recv_v_then_yumi_p(recv_v_then_yumi_p), .recv_ready_then_v_p(recv_ready_then_v_p), .recv_v_and_retry_p(recv_v_and_retry_p), .recv_v_then_retry_p(recv_v_then_retry_p), .width_p(width_p)) i_assertions (.*);