module bsg_cache_decode_assertions
  import bsg_cache_pkg::*;
  (
    input bsg_cache_opcode_e opcode_i,
    input bsg_cache_decode_s decode_o
  );

  logic [1:0] expected_data_size_op;
  logic expected_mask_op;
  logic expected_sigext_op;
  logic expected_ld_op;
  logic expected_st_op;
  logic expected_tagst_op;
  logic expected_tagfl_op;
  logic expected_taglv_op;
  logic expected_tagla_op;
  logic expected_afl_op;
  logic expected_aflinv_op;
  logic expected_ainv_op;
  logic expected_alock_op;
  logic expected_aunlock_op;
  logic expected_tag_read_op;
  logic expected_atomic_op;

  always_comb begin : auxiliary_rtl_logic
    case (opcode_i)
      AMOSWAP_D, AMOADD_D, AMOAND_D, AMOOR_D, AMOXOR_D,
      AMOMIN_D, AMOMAX_D, AMOMINU_D, AMOMAXU_D,
      LD, SD, LDU: expected_data_size_op = 2'b11;

      AMOSWAP_W, AMOADD_W, AMOAND_W, AMOOR_W, AMOXOR_W,
      AMOMIN_W, AMOMAX_W, AMOMINU_W, AMOMAXU_W,
      LW, SW, LWU: expected_data_size_op = 2'b10;

      LH, SH, LHU: expected_data_size_op = 2'b01;

      LB, SB, LBU: expected_data_size_op = 2'b00;

      default: expected_data_size_op = 2'b00;
    endcase

    expected_atomic_op = (opcode_i == AMOSWAP_W) || (opcode_i == AMOSWAP_D)
      || (opcode_i == AMOADD_W) || (opcode_i == AMOADD_D)
      || (opcode_i == AMOXOR_W) || (opcode_i == AMOXOR_D)
      || (opcode_i == AMOAND_W) || (opcode_i == AMOAND_D)
      || (opcode_i == AMOOR_W) || (opcode_i == AMOOR_D)
      || (opcode_i == AMOMIN_W) || (opcode_i == AMOMIN_D)
      || (opcode_i == AMOMAX_W) || (opcode_i == AMOMAX_D)
      || (opcode_i == AMOMINU_W) || (opcode_i == AMOMINU_D)
      || (opcode_i == AMOMAXU_W) || (opcode_i == AMOMAXU_D);

    expected_mask_op = (opcode_i == LM) || (opcode_i == SM);

    expected_sigext_op = (opcode_i == LB) || (opcode_i == LH)
      || (opcode_i == LW) || (opcode_i == LD) || expected_atomic_op;

    expected_ld_op = (opcode_i == LB) || (opcode_i == LH)
      || (opcode_i == LW) || (opcode_i == LD)
      || (opcode_i == LBU) || (opcode_i == LHU)
      || (opcode_i == LWU) || (opcode_i == LDU)
      || (opcode_i == LM);

    expected_st_op = (opcode_i == SB) || (opcode_i == SH)
      || (opcode_i == SW) || (opcode_i == SD)
      || (opcode_i == SM);

    expected_tagst_op = (opcode_i == TAGST);
    expected_tagfl_op = (opcode_i == TAGFL);
    expected_taglv_op = (opcode_i == TAGLV);
    expected_tagla_op = (opcode_i == TAGLA);
    expected_afl_op = (opcode_i == AFL);
    expected_aflinv_op = (opcode_i == AFLINV);
    expected_ainv_op = (opcode_i == AINV);
    expected_alock_op = (opcode_i == ALOCK);
    expected_aunlock_op = (opcode_i == AUNLOCK);
    expected_tag_read_op = (opcode_i != TAGST);
  end

  always_comb begin : field_exact_assertions
    a_data_size_op_exact: assert (decode_o.data_size_op == expected_data_size_op);
    a_mask_op_exact: assert (decode_o.mask_op == expected_mask_op);
    a_sigext_op_exact: assert (decode_o.sigext_op == expected_sigext_op);
    a_ld_op_exact: assert (decode_o.ld_op == expected_ld_op);
    a_st_op_exact: assert (decode_o.st_op == expected_st_op);
    a_tagst_op_exact: assert (decode_o.tagst_op == expected_tagst_op);
    a_tagfl_op_exact: assert (decode_o.tagfl_op == expected_tagfl_op);
    a_taglv_op_exact: assert (decode_o.taglv_op == expected_taglv_op);
    a_tagla_op_exact: assert (decode_o.tagla_op == expected_tagla_op);
    a_afl_op_exact: assert (decode_o.afl_op == expected_afl_op);
    a_aflinv_op_exact: assert (decode_o.aflinv_op == expected_aflinv_op);
    a_ainv_op_exact: assert (decode_o.ainv_op == expected_ainv_op);
    a_alock_op_exact: assert (decode_o.alock_op == expected_alock_op);
    a_aunlock_op_exact: assert (decode_o.aunlock_op == expected_aunlock_op);
    a_tag_read_op_exact: assert (decode_o.tag_read_op == expected_tag_read_op);
    a_atomic_op_exact: assert (decode_o.atomic_op == expected_atomic_op);
  end

  always_comb begin : amo_subop_assertions
    a_amo_subop_exact: assert (
         (((opcode_i == AMOSWAP_W) || (opcode_i == AMOSWAP_D)) && (decode_o.amo_subop == e_cache_amo_swap))
      || (((opcode_i == AMOADD_W)  || (opcode_i == AMOADD_D))  && (decode_o.amo_subop == e_cache_amo_add))
      || (((opcode_i == AMOXOR_W)  || (opcode_i == AMOXOR_D))  && (decode_o.amo_subop == e_cache_amo_xor))
      || (((opcode_i == AMOAND_W)  || (opcode_i == AMOAND_D))  && (decode_o.amo_subop == e_cache_amo_and))
      || (((opcode_i == AMOOR_W)   || (opcode_i == AMOOR_D))   && (decode_o.amo_subop == e_cache_amo_or))
      || (((opcode_i == AMOMIN_W)  || (opcode_i == AMOMIN_D))  && (decode_o.amo_subop == e_cache_amo_min))
      || (((opcode_i == AMOMAX_W)  || (opcode_i == AMOMAX_D))  && (decode_o.amo_subop == e_cache_amo_max))
      || (((opcode_i == AMOMINU_W) || (opcode_i == AMOMINU_D)) && (decode_o.amo_subop == e_cache_amo_minu))
      || (((opcode_i == AMOMAXU_W) || (opcode_i == AMOMAXU_D)) && (decode_o.amo_subop == e_cache_amo_maxu))
      || ((!expected_atomic_op) && (decode_o.amo_subop == e_cache_amo_swap))
    );
  end

  always_comb begin : cross_field_assertions
    a_tag_read_inverse_tagst: assert (decode_o.tag_read_op == ~decode_o.tagst_op);
    a_load_store_mutually_exclusive: assert (!(decode_o.ld_op && decode_o.st_op));
    a_mask_is_load_or_store: assert (!decode_o.mask_op || (decode_o.ld_op || decode_o.st_op));
    a_atomic_implies_sigext: assert (!decode_o.atomic_op || decode_o.sigext_op);
    a_atomic_not_load_store_mask: assert (!decode_o.atomic_op || (!decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op));
    a_tagst_disables_tag_read: assert (!decode_o.tagst_op || !decode_o.tag_read_op);
  end

  always_comb begin : decode_cover_points
    c_data_size_double_seen: cover (decode_o.data_size_op == 2'b11);
    c_data_size_word_seen: cover (decode_o.data_size_op == 2'b10);
    c_data_size_half_seen: cover (decode_o.data_size_op == 2'b01);
    c_data_size_byte_seen: cover (decode_o.data_size_op == 2'b00);
    c_mask_op_seen: cover (decode_o.mask_op);
    c_sigext_op_seen: cover (decode_o.sigext_op);
    c_ld_op_seen: cover (decode_o.ld_op);
    c_st_op_seen: cover (decode_o.st_op);
    c_tagst_op_seen: cover (decode_o.tagst_op);
    c_tagfl_op_seen: cover (decode_o.tagfl_op);
    c_taglv_op_seen: cover (decode_o.taglv_op);
    c_tagla_op_seen: cover (decode_o.tagla_op);
    c_afl_op_seen: cover (decode_o.afl_op);
    c_aflinv_op_seen: cover (decode_o.aflinv_op);
    c_ainv_op_seen: cover (decode_o.ainv_op);
    c_alock_op_seen: cover (decode_o.alock_op);
    c_aunlock_op_seen: cover (decode_o.aunlock_op);
    c_tag_read_op_seen: cover (decode_o.tag_read_op);
    c_tag_read_op_clear_seen: cover (!decode_o.tag_read_op);
    c_atomic_op_seen: cover (decode_o.atomic_op);
    c_non_atomic_op_seen: cover (!decode_o.atomic_op);
    c_amo_swap_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_swap));
    c_amo_add_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_add));
    c_amo_xor_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_xor));
    c_amo_and_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_and));
    c_amo_or_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_or));
    c_amo_min_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_min));
    c_amo_max_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_max));
    c_amo_minu_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_minu));
    c_amo_maxu_seen: cover (decode_o.atomic_op && (decode_o.amo_subop == e_cache_amo_maxu));
  end

endmodule

bind bsg_cache_decode bsg_cache_decode_assertions i_assertions (.*);