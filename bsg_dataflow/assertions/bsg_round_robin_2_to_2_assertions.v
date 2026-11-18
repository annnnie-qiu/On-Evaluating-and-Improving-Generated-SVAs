module bsg_round_robin_2_to_2_assertions #(parameter width_p = 8)
  (input clk_i
   , input reset_i
   , input [width_p*2-1:0] data_i
   , input [1:0] v_i
   , input [1:0] ready_o
   , input [width_p*2-1:0] data_o
   , input [1:0] v_o
   , input [1:0] ready_i
   );

  wire [width_p*2-1:0] data_i_swapped;
  wire [1:0] v_i_swapped;
  wire [1:0] ready_i_swapped;

  assign data_i_swapped = {data_i[0+:width_p], data_i[width_p+:width_p]};
  assign v_i_swapped = {v_i[0], v_i[1]};
  assign ready_i_swapped = {ready_i[0], ready_i[1]};

  wire orientation0;
  wire orientation1;
  wire orientation_valid;
  wire unique_orientation0;
  wire unique_orientation1;

  assign orientation0 = (data_o == data_i) && (v_o == v_i) && (ready_o == ready_i);
  assign orientation1 = (data_o == data_i_swapped) && (v_o == v_i_swapped) && (ready_o == ready_i_swapped);
  assign orientation_valid = orientation0 || orientation1;
  assign unique_orientation0 = orientation0 && !orientation1;
  assign unique_orientation1 = orientation1 && !orientation0;

  wire [1:0] input_fire;
  wire [1:0] output_fire;
  wire accept_odd;
  wire [1:0] input_fire_count;
  wire [1:0] output_fire_count;

  assign input_fire = v_i & ready_o;
  assign output_fire = v_o & ready_i;
  assign accept_odd = ^input_fire;
  assign input_fire_count = {1'b0, input_fire[0]} + {1'b0, input_fire[1]};
  assign output_fire_count = {1'b0, output_fire[0]} + {1'b0, output_fire[1]};

  wire [width_p-1:0] data_i_lane_xor;
  wire [width_p-1:0] data_o_lane_xor;
  assign data_i_lane_xor = data_i[0+:width_p] ^ data_i[width_p+:width_p];
  assign data_o_lane_xor = data_o[0+:width_p] ^ data_o[width_p+:width_p];

  begin : combinational_mapping_assertions
    a_common_orientation_valid: assert property (@(posedge clk_i) disable iff (reset_i)
      orientation_valid
    );

    a_v_multiset_preserved: assert property (@(posedge clk_i) disable iff (reset_i)
      (({1'b0, v_o[0]} + {1'b0, v_o[1]}) == ({1'b0, v_i[0]} + {1'b0, v_i[1]}))
    );

    a_ready_multiset_preserved: assert property (@(posedge clk_i) disable iff (reset_i)
      (({1'b0, ready_o[0]} + {1'b0, ready_o[1]}) == ({1'b0, ready_i[0]} + {1'b0, ready_i[1]}))
    );

    a_data_lane_xor_preserved: assert property (@(posedge clk_i) disable iff (reset_i)
      (data_o_lane_xor == data_i_lane_xor)
    );

    a_fire_parity_preserved: assert property (@(posedge clk_i) disable iff (reset_i)
      ((^input_fire) == (^output_fire))
    );

    a_fire_count_preserved: assert property (@(posedge clk_i) disable iff (reset_i)
      (input_fire_count == output_fire_count)
    );
  end

  begin : sequential_round_robin_assertions
    a_hold_unswapped_on_even_accepts: assert property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation0 && !accept_odd) |=> orientation0
    );

    a_toggle_unswapped_on_odd_accepts: assert property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation0 && accept_odd) |=> orientation1
    );

    a_hold_swapped_on_even_accepts: assert property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation1 && !accept_odd) |=> orientation1
    );

    a_toggle_swapped_on_odd_accepts: assert property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation1 && accept_odd) |=> orientation0
    );
  end

  begin : functional_covers
    c_orientation_unswapped: cover property (@(posedge clk_i) disable iff (reset_i)
      orientation0
    );

    c_orientation_swapped: cover property (@(posedge clk_i) disable iff (reset_i)
      orientation1
    );

    c_unique_orientation_unswapped: cover property (@(posedge clk_i) disable iff (reset_i)
      unique_orientation0
    );

    c_unique_orientation_swapped: cover property (@(posedge clk_i) disable iff (reset_i)
      unique_orientation1
    );

    c_no_accept: cover property (@(posedge clk_i) disable iff (reset_i)
      (input_fire_count == 2'd0)
    );

    c_single_accept: cover property (@(posedge clk_i) disable iff (reset_i)
      (input_fire_count == 2'd1)
    );

    c_double_accept: cover property (@(posedge clk_i) disable iff (reset_i)
      (input_fire_count == 2'd2)
    );

    c_unswapped_to_swapped_toggle: cover property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation0 && accept_odd) ##1 orientation1
    );

    c_swapped_to_unswapped_toggle: cover property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation1 && accept_odd) ##1 orientation0
    );

    c_unswapped_hold: cover property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation0 && !accept_odd) ##1 orientation0
    );

    c_swapped_hold: cover property (@(posedge clk_i) disable iff (reset_i)
      (unique_orientation1 && !accept_odd) ##1 orientation1
    );
  end

endmodule

bind bsg_round_robin_2_to_2 bsg_round_robin_2_to_2_assertions #(.width_p(width_p)) i_assertions (.*);