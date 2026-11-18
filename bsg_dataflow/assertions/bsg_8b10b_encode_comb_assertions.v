module bsg_8b10b_encode_comb_assertions
(
  input        [7:0] data_i,
  input              k_i,
  input              rd_i,
  input        [9:0] data_o,
  input              rd_o,
  input              kerr_o
);

  function automatic [3:0] popcount10(input [9:0] v);
    integer i;
    begin
      popcount10 = 4'd0;
      for (i = 0; i < 10; i = i + 1) begin
        popcount10 = popcount10 + v[i];
      end
    end
  endfunction

  wire A = data_i[0];
  wire B = data_i[1];
  wire C = data_i[2];
  wire D = data_i[3];
  wire E = data_i[4];
  wire F = data_i[5];
  wire G = data_i[6];
  wire H = data_i[7];

  wire AxorB = A ^ B;
  wire CxorD = C ^ D;
  wire AandB = A & B;
  wire CandD = C & D;
  wire NAandNB = ~A & ~B;
  wire NCandND = ~C & ~D;

  wire L22 = (AandB & NCandND) | (CandD & NAandNB) | (AxorB & CxorD);
  wire L40 = AandB & CandD;
  wire L04 = NAandNB & NCandND;
  wire L13 = (AxorB & NCandND) | (CxorD & NAandNB);
  wire L31 = (AxorB & CandD) | (CxorD & AandB);

  wire FxorG = F ^ G;
  wire FandG = F & G;
  wire NFandNG = ~F & ~G;
  wire NFandNGandNH = NFandNG & ~H;
  wire FxorGandK = FxorG & k_i;
  wire FxorGandNH = FxorG & ~H;
  wire FandGandH = FandG & H;

  wire S = (rd_i & L31 & D & ~E) | (~rd_i & L13 & ~D & E);
  wire T0 = L13 & D & E;

  wire PDM1S6 = T0 | (~L22 & ~L31 & ~E);
  wire ND0S6 = PDM1S6;
  wire PD0S6 = (E & ~L22 & ~L13) | k_i;
  wire NDM1S6 = (L31 & ~D & ~E) | PD0S6;
  wire NDM1S4 = FandG;
  wire ND0S4 = NFandNG;
  wire PDM1S4 = NFandNG | FxorGandK;
  wire PD0S4 = FandGandH;

  wire COMPLS6 = (NDM1S6 & rd_i) | (~rd_i & PDM1S6);
  wire NDL6 = (PD0S6 & ~COMPLS6) | (COMPLS6 & ND0S6) | (~ND0S6 & ~PD0S6 & rd_i);
  wire COMPLS4 = (NDM1S4 & NDL6) | (~NDL6 & PDM1S4);

  wire expected_rd_o = (NDL6 & ~PD0S4 & ~ND0S4) | (ND0S4 & COMPLS4) | (~COMPLS4 & PD0S4);

  wire N0 = A;
  wire N1 = (~L40 & B) | L04;
  wire N2 = (L04 | C) | T0;
  wire N3 = D & ~L40;
  wire N4 = (~T0 & E) | (~E & L13);
  wire N5 = (~E & L22) | (L22 & k_i) | (L04 & E) | (E & L40) | (E & L13 & ~D);

  wire T1 = (S & FandGandH) | (FandGandH & k_i);

  wire N6 = ~(~F | T1);
  wire N7 = G | NFandNGandNH;
  wire N8 = H;
  wire N9 = T1 | FxorGandNH;

  wire [5:0] expected_data_o_lower = {N5, N4, N3, N2, N1, N0} ^ {6{COMPLS6}};
  wire [3:0] expected_data_o_upper = {N9, N8, N7, N6} ^ {4{COMPLS4}};
  wire [9:0] expected_data_o = {expected_data_o_upper, expected_data_o_lower};

  wire legal_k_formula = (NAandNB & CandD & E) | (FandGandH & E & L31);
  wire legal_k_enumerated = (data_i[4:0] == 5'b11100)
                          | ((data_i[7:5] == 3'b111)
                             & ((data_i[4:0] == 5'b10111)
                              | (data_i[4:0] == 5'b11011)
                              | (data_i[4:0] == 5'b11101)
                              | (data_i[4:0] == 5'b11110)));
  wire expected_kerr_o = k_i & ~legal_k_formula;

  wire [3:0] data_o_ones = popcount10(data_o);

  always_comb begin : output_equation_assertions
    a_data_o_matches_full_equation: assert (data_o == expected_data_o);
    a_data_o_lower_six_matches_equation: assert (data_o[5:0] == expected_data_o_lower);
    a_data_o_upper_four_matches_equation: assert (data_o[9:6] == expected_data_o_upper);
    a_rd_o_matches_equation: assert (rd_o == expected_rd_o);
    a_kerr_o_matches_formula: assert (kerr_o == expected_kerr_o);
    a_kerr_o_matches_enumerated_legal_k: assert (kerr_o == (k_i & ~legal_k_enumerated));
  end

  always_comb begin : output_bit_assertions
    a_data_o_bit0_matches: assert (data_o[0] == (N0 ^ COMPLS6));
    a_data_o_bit1_matches: assert (data_o[1] == (N1 ^ COMPLS6));
    a_data_o_bit2_matches: assert (data_o[2] == (N2 ^ COMPLS6));
    a_data_o_bit3_matches: assert (data_o[3] == (N3 ^ COMPLS6));
    a_data_o_bit4_matches: assert (data_o[4] == (N4 ^ COMPLS6));
    a_data_o_bit5_matches: assert (data_o[5] == (N5 ^ COMPLS6));
    a_data_o_bit6_matches: assert (data_o[6] == (N6 ^ COMPLS4));
    a_data_o_bit7_matches: assert (data_o[7] == (N7 ^ COMPLS4));
    a_data_o_bit8_matches: assert (data_o[8] == (N8 ^ COMPLS4));
    a_data_o_bit9_matches: assert (data_o[9] == (N9 ^ COMPLS4));
  end

  always_comb begin : k_character_assertions
    a_data_character_never_reports_kerr: assert (k_i || !kerr_o);
    a_valid_k_character_no_kerr: assert (!k_i || !legal_k_enumerated || !kerr_o);
    a_invalid_k_character_reports_kerr: assert (!k_i || legal_k_enumerated || kerr_o);
  end

  always_comb begin : disparity_assertions
    a_legal_symbol_disparity_within_two: assert (kerr_o || ((data_o_ones >= 4'd4) && (data_o_ones <= 4'd6)));
    a_legal_positive_disparity_sets_rd_o: assert (kerr_o || (data_o_ones <= 4'd5) || rd_o);
    a_legal_negative_disparity_clears_rd_o: assert (kerr_o || (data_o_ones >= 4'd5) || !rd_o);
    a_legal_neutral_disparity_keeps_rd: assert (kerr_o || (data_o_ones != 4'd5) || (rd_o == rd_i));
  end

  always_comb begin : output_cover_checks
    c_cover_rd_o_high: cover (rd_o);
    c_cover_rd_o_low: cover (!rd_o);
    c_cover_kerr_asserted: cover (kerr_o);
    c_cover_kerr_clear: cover (!kerr_o);
    c_cover_legal_negative_disparity_symbol: cover (!kerr_o && (data_o_ones == 4'd4));
    c_cover_legal_neutral_disparity_symbol: cover (!kerr_o && (data_o_ones == 4'd5));
    c_cover_legal_positive_disparity_symbol: cover (!kerr_o && (data_o_ones == 4'd6));
    c_cover_lower_six_complemented: cover (data_o[5:0] == (~{N5, N4, N3, N2, N1, N0}));
    c_cover_upper_four_complemented: cover (data_o[9:6] == (~{N9, N8, N7, N6}));
  end

endmodule

bind bsg_8b10b_encode_comb bsg_8b10b_encode_comb_assertions i_assertions (.*);