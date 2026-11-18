/**
 *  bsg_nonsynth_cache_axe_tracer.sv
 *
 *  use SystemVerilog 'bind' on bsg_cache.
 *
 *
 *  @author anonymized
 *
 */



module bsg_nonsynth_cache_axe_tracer
  import bsg_cache_pkg::*;
  #(parameter data_width_p = 8
    , parameter addr_width_p = 8
    , parameter ways_p = 8
    , parameter sbuf_entry_width_lp=
    (addr_width_p+data_width_p+(data_width_p>>3)+( (((ways_p)==1) || ((ways_p)==0))? 1 : $clog2((ways_p))))
  )
  (
    input clk_i
    , input v_o
    , input yumi_i
    , input bsg_cache_decode_s decode_v_r
    , input [addr_width_p-1:0] addr_v_r
    , input [sbuf_entry_width_lp-1:0] sbuf_entry_li
    , input [data_width_p-1:0] data_o
  );

    typedef struct packed {
      logic [addr_width_p-1:0] addr;
      logic [data_width_p-1:0] data;
      logic [(data_width_p>>3)-1:0] mask;
      logic [( (((ways_p)==1) || ((ways_p)==0))? 1 : $clog2((ways_p)))-1:0] way_id;
    } bsg_cache_sbuf_entry_s;
  bsg_cache_sbuf_entry_s sbuf_entry;
  assign sbuf_entry = sbuf_entry_li;

endmodule
