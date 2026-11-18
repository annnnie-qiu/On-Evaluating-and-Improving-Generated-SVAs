module bsg_cache_non_blocking_decode_assertions
  import bsg_cache_non_blocking_pkg::*;
  (
    input bsg_cache_non_blocking_opcode_e opcode_i
    , input bsg_cache_non_blocking_decode_s decode_o
  );

  always_comb begin : decode_function_assertions
    a_size_op_exact: assert (decode_o.size_op == (((opcode_i == LD) || (opcode_i == SD)) ? 2'b11
                                      : (((opcode_i == LW) || (opcode_i == SW) || (opcode_i == LWU)) ? 2'b10
                                      : (((opcode_i == LH) || (opcode_i == SH) || (opcode_i == LHU)) ? 2'b01
                                      : 2'b00))));

    a_sigext_op_exact: assert (decode_o.sigext_op == ((opcode_i == LB)
                                                || (opcode_i == LH)
                                                || (opcode_i == LW)
                                                || (opcode_i == LD)));

    a_ld_op_exact: assert (decode_o.ld_op == ((opcode_i == LB)
                                       || (opcode_i == LH)
                                       || (opcode_i == LW)
                                       || (opcode_i == LD)
                                       || (opcode_i == LBU)
                                       || (opcode_i == LHU)
                                       || (opcode_i == LWU)));

    a_st_op_exact: assert (decode_o.st_op == ((opcode_i == SB)
                                       || (opcode_i == SH)
                                       || (opcode_i == SW)
                                       || (opcode_i == SD)
                                       || (opcode_i == SM)));

    a_mask_op_exact: assert (decode_o.mask_op == (opcode_i == SM));
    a_block_ld_op_exact: assert (decode_o.block_ld_op == (opcode_i == BLOCK_LD));

    a_tagst_op_exact: assert (decode_o.tagst_op == (opcode_i == TAGST));
    a_tagfl_op_exact: assert (decode_o.tagfl_op == (opcode_i == TAGFL));
    a_taglv_op_exact: assert (decode_o.taglv_op == (opcode_i == TAGLV));
    a_tagla_op_exact: assert (decode_o.tagla_op == (opcode_i == TAGLA));
    a_afl_op_exact: assert (decode_o.afl_op == (opcode_i == AFL));
    a_aflinv_op_exact: assert (decode_o.aflinv_op == (opcode_i == AFLINV));
    a_ainv_op_exact: assert (decode_o.ainv_op == (opcode_i == AINV));
    a_alock_op_exact: assert (decode_o.alock_op == (opcode_i == ALOCK));
    a_aunlock_op_exact: assert (decode_o.aunlock_op == (opcode_i == AUNLOCK));

    // Additional opcode consistency checks.
    a_opcode_guard_consistency: assert (opcode_guard_valid == decode_o.mgmt_op);
    a_load_opcode_requires_store_class: assert ((opcode_i != LD) || decode_o.st_op);

    a_mgmt_op_exact: assert (decode_o.mgmt_op == ~(((opcode_i == LB)
                                             || (opcode_i == LH)
                                             || (opcode_i == LW)
                                             || (opcode_i == LD)
                                             || (opcode_i == LBU)
                                             || (opcode_i == LHU)
                                             || (opcode_i == LWU)
                                             || (opcode_i == SB)
                                             || (opcode_i == SH)
                                             || (opcode_i == SW)
                                             || (opcode_i == SD)
                                             || (opcode_i == SM)
                                             || (opcode_i == BLOCK_LD))));
  end

  always_comb begin : decode_consistency_assertions
    a_mgmt_op_consistent: assert (decode_o.mgmt_op == ~(decode_o.ld_op | decode_o.st_op | decode_o.block_ld_op));
    a_sigext_implies_load: assert (!decode_o.sigext_op || decode_o.ld_op);
    a_mask_implies_store: assert (!decode_o.mask_op || decode_o.st_op);
    a_primary_class_onehot0: assert ($onehot0({decode_o.ld_op, decode_o.st_op, decode_o.block_ld_op}));
    a_mask_not_load_or_block: assert (!decode_o.mask_op || (!decode_o.ld_op && !decode_o.block_ld_op));
    a_block_not_load_or_store: assert (!decode_o.block_ld_op || (!decode_o.ld_op && !decode_o.st_op));
    a_tagst_implies_mgmt: assert (!decode_o.tagst_op || decode_o.mgmt_op);
    a_tagfl_implies_mgmt: assert (!decode_o.tagfl_op || decode_o.mgmt_op);
    a_taglv_implies_mgmt: assert (!decode_o.taglv_op || decode_o.mgmt_op);
    a_tagla_implies_mgmt: assert (!decode_o.tagla_op || decode_o.mgmt_op);
    a_afl_implies_mgmt: assert (!decode_o.afl_op || decode_o.mgmt_op);
    a_aflinv_implies_mgmt: assert (!decode_o.aflinv_op || decode_o.mgmt_op);
    a_ainv_implies_mgmt: assert (!decode_o.ainv_op || decode_o.mgmt_op);
    a_alock_implies_mgmt: assert (!decode_o.alock_op || decode_o.mgmt_op);
    a_aunlock_implies_mgmt: assert (!decode_o.aunlock_op || decode_o.mgmt_op);
  end

  always_comb begin : opcode_specific_assertions
    a_lb_decode: assert ((opcode_i != LB) || (decode_o.size_op == 2'b00 && decode_o.sigext_op && decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_lh_decode: assert ((opcode_i != LH) || (decode_o.size_op == 2'b01 && decode_o.sigext_op && decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_lw_decode: assert ((opcode_i != LW) || (decode_o.size_op == 2'b10 && decode_o.sigext_op && decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_ld_decode: assert ((opcode_i != LD) || (decode_o.size_op == 2'b11 && decode_o.sigext_op && decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_lbu_decode: assert ((opcode_i != LBU) || (decode_o.size_op == 2'b00 && !decode_o.sigext_op && decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_lhu_decode: assert ((opcode_i != LHU) || (decode_o.size_op == 2'b01 && !decode_o.sigext_op && decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_lwu_decode: assert ((opcode_i != LWU) || (decode_o.size_op == 2'b10 && !decode_o.sigext_op && decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_sb_decode: assert ((opcode_i != SB) || (decode_o.size_op == 2'b00 && !decode_o.sigext_op && !decode_o.ld_op && decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_sh_decode: assert ((opcode_i != SH) || (decode_o.size_op == 2'b01 && !decode_o.sigext_op && !decode_o.ld_op && decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_sw_decode: assert ((opcode_i != SW) || (decode_o.size_op == 2'b10 && !decode_o.sigext_op && !decode_o.ld_op && decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_sd_decode: assert ((opcode_i != SD) || (decode_o.size_op == 2'b11 && !decode_o.sigext_op && !decode_o.ld_op && decode_o.st_op && !decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_sm_decode: assert ((opcode_i != SM) || (decode_o.size_op == 2'b00 && !decode_o.sigext_op && !decode_o.ld_op && decode_o.st_op && decode_o.mask_op && !decode_o.block_ld_op && !decode_o.mgmt_op));
    a_block_ld_decode: assert ((opcode_i != BLOCK_LD) || (decode_o.size_op == 2'b00 && !decode_o.sigext_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.mask_op && decode_o.block_ld_op && !decode_o.mgmt_op));
    a_tagst_decode: assert ((opcode_i != TAGST) || (decode_o.mgmt_op && decode_o.tagst_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_tagfl_decode: assert ((opcode_i != TAGFL) || (decode_o.mgmt_op && decode_o.tagfl_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_taglv_decode: assert ((opcode_i != TAGLV) || (decode_o.mgmt_op && decode_o.taglv_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_tagla_decode: assert ((opcode_i != TAGLA) || (decode_o.mgmt_op && decode_o.tagla_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_afl_decode: assert ((opcode_i != AFL) || (decode_o.mgmt_op && decode_o.afl_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_aflinv_decode: assert ((opcode_i != AFLINV) || (decode_o.mgmt_op && decode_o.aflinv_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_ainv_decode: assert ((opcode_i != AINV) || (decode_o.mgmt_op && decode_o.ainv_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_alock_decode: assert ((opcode_i != ALOCK) || (decode_o.mgmt_op && decode_o.alock_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
    a_aunlock_decode: assert ((opcode_i != AUNLOCK) || (decode_o.mgmt_op && decode_o.aunlock_op && !decode_o.ld_op && !decode_o.st_op && !decode_o.block_ld_op));
  end

  always_comb begin : decode_coverpoints
    c_size_op_00: cover (decode_o.size_op == 2'b00);
    c_size_op_01: cover (decode_o.size_op == 2'b01);
    c_size_op_10: cover (decode_o.size_op == 2'b10);
    c_size_op_11: cover (decode_o.size_op == 2'b11);
    c_sigext_op: cover (decode_o.sigext_op);
    c_ld_op: cover (decode_o.ld_op);
    c_st_op: cover (decode_o.st_op);
    c_mask_op: cover (decode_o.mask_op);
    c_block_ld_op: cover (decode_o.block_ld_op);
    c_mgmt_op: cover (decode_o.mgmt_op);
    c_tagst_op: cover (decode_o.tagst_op);
    c_tagfl_op: cover (decode_o.tagfl_op);
    c_taglv_op: cover (decode_o.taglv_op);
    c_tagla_op: cover (decode_o.tagla_op);
    c_afl_op: cover (decode_o.afl_op);
    c_aflinv_op: cover (decode_o.aflinv_op);
    c_ainv_op: cover (decode_o.ainv_op);
    c_alock_op: cover (decode_o.alock_op);
    c_aunlock_op: cover (decode_o.aunlock_op);
  end

endmodule

bind bsg_cache_non_blocking_decode bsg_cache_non_blocking_decode_assertions i_assertions (.*);
