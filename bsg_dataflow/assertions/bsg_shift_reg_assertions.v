module bsg_shift_reg_assertions #(
    parameter width_p = 8,
    parameter stages_p = 8
)(
    input clk,
    input reset_i,
    input valid_i,
    input [width_p-1:0] data_i,
    input valid_o,
    input [width_p-1:0] data_o
);

  generate
    begin : fixed_latency_assertions

      property p_payload_fixed_latency;
        logic [width_p:0] payload_sample;
        @(posedge clk) disable iff (reset_i)
          (1'b1, payload_sample = {valid_i, data_i}) |-> ##stages_p ({valid_o, data_o} == payload_sample);
      endproperty
      a_payload_fixed_latency: assert property (p_payload_fixed_latency);

      property p_valid_fixed_latency;
        logic valid_sample;
        @(posedge clk) disable iff (reset_i)
          (1'b1, valid_sample = valid_i) |-> ##stages_p (valid_o == valid_sample);
      endproperty
      a_valid_fixed_latency: assert property (p_valid_fixed_latency);

      property p_data_fixed_latency;
        logic [width_p-1:0] data_sample;
        @(posedge clk) disable iff (reset_i)
          (1'b1, data_sample = data_i) |-> ##stages_p (data_o == data_sample);
      endproperty
      a_data_fixed_latency: assert property (p_data_fixed_latency);

      property p_valid_high_propagates;
        @(posedge clk) disable iff (reset_i)
          valid_i |-> ##stages_p valid_o;
      endproperty
      a_valid_high_propagates: assert property (p_valid_high_propagates);

      property p_valid_low_propagates;
        @(posedge clk) disable iff (reset_i)
          (!valid_i) |-> ##stages_p (!valid_o);
      endproperty
      a_valid_low_propagates: assert property (p_valid_low_propagates);

      c_valid_o_high: cover property (@(posedge clk) disable iff (reset_i) valid_o);
      c_valid_o_low: cover property (@(posedge clk) disable iff (reset_i) !valid_o);
      c_valid_o_rise: cover property (@(posedge clk) disable iff (reset_i) (!valid_o ##1 valid_o));
      c_valid_o_fall: cover property (@(posedge clk) disable iff (reset_i) (valid_o ##1 !valid_o));
      c_data_o_nonzero: cover property (@(posedge clk) disable iff (reset_i) (data_o != {width_p{1'b0}}));
      c_data_o_all_ones: cover property (@(posedge clk) disable iff (reset_i) (data_o == {width_p{1'b1}}));
      c_payload_o_nonzero: cover property (@(posedge clk) disable iff (reset_i) ({valid_o, data_o} != {(width_p+1){1'b0}}));

    end : fixed_latency_assertions
  endgenerate

endmodule

bind bsg_shift_reg bsg_shift_reg_assertions #(.width_p(width_p), .stages_p(stages_p)) i_assertions (.*);