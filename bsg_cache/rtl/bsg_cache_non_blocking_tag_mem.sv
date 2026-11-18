/**
 *    bsg_cache_non_blocking_tag_mem.sv
 *
 *    @author anonymized
 *
 */



module bsg_cache_non_blocking_tag_mem 
  import bsg_cache_non_blocking_pkg::*;
  #(parameter sets_p = 8
    , parameter ways_p = 8
    , parameter tag_width_p = 8
    , parameter data_width_p = 8

    , parameter lg_ways_lp=( (((ways_p)==1) || ((ways_p)==0))? 1 : $clog2((ways_p)))
    , parameter lg_sets_lp=( (((sets_p)==1) || ((sets_p)==0))? 1 : $clog2((sets_p)))

    , parameter debug_p=0

    , parameter tag_mem_pkt_width_lp=

    (( (((ways_p)==1) || ((ways_p)==0))? 1 : $clog2((ways_p)))+( (((sets_p)==1) || ((sets_p)==0))? 1 : $clog2((sets_p)))+data_width_p+tag_width_p+$bits(bsg_cache_non_blocking_tag_op_e))
  )
  (
    input clk_i
    , input reset_i

    , input v_i
    , input [tag_mem_pkt_width_lp-1:0] tag_mem_pkt_i

    , output logic [ways_p-1:0] valid_o
    , output logic [ways_p-1:0] lock_o
    , output logic [ways_p-1:0][tag_width_p-1:0] tag_o
  );

  // localparam
  //
  localparam tag_info_width_lp = 
    (tag_width_p+2);

  // tag_mem pkt
  //

    typedef struct packed {
      logic [( (((ways_p)==1) || ((ways_p)==0))? 1 : $clog2((ways_p)))-1:0] way_id;
      logic [( (((sets_p)==1) || ((sets_p)==0))? 1 : $clog2((sets_p)))-1:0] index;
      logic [data_width_p-1:0] data;
      logic [tag_width_p-1:0] tag;
      bsg_cache_non_blocking_tag_op_e opcode;
    } bsg_cache_non_blocking_tag_mem_pkt_s;

  bsg_cache_non_blocking_tag_mem_pkt_s tag_mem_pkt;

  assign tag_mem_pkt = tag_mem_pkt_i;

  // tag_mem
  //

    typedef struct packed {
      logic valid;
      logic lock;
      logic [tag_width_p-1:0] tag;
    } bsg_cache_non_blocking_tag_info_s;

  logic w_li;
  bsg_cache_non_blocking_tag_info_s [ways_p-1:0] mask_li, data_li, data_lo;

  bsg_mem_1rw_sync_mask_write_bit #(
    .width_p(tag_info_width_lp*ways_p)
    ,.els_p(sets_p)
    ,.latch_last_read_p(1)
  ) tag_mem0 (
    .clk_i(clk_i)
    ,.reset_i(reset_i)

    ,.v_i(v_i)
    ,.w_i(w_li)

    ,.addr_i(tag_mem_pkt.index)
    ,.w_mask_i(mask_li)
    ,.data_i(data_li)
    ,.data_o(data_lo)
  );

  // input logic
  //
  logic [ways_p-1:0] way_decode;

  bsg_decode #(
    .num_out_p(ways_p)
  ) way_demux (
    .i(tag_mem_pkt.way_id)
    ,.o(way_decode)
  );

  always_comb begin

    w_li = 1'b0;
    data_li = '0;
    mask_li = '0;

    case (tag_mem_pkt.opcode)

      // read tags for given index.
      e_tag_read: begin
        w_li = 1'b0;
        data_li = '0;
        mask_li = '0;
      end

      // TAGST
      e_tag_store: begin 
        w_li = 1'b1;
        for (integer i = 0 ; i < ways_p; i++) begin
          data_li[i].tag = tag_mem_pkt.data[0+:tag_width_p];
          data_li[i].valid = tag_mem_pkt.data[data_width_p-1];
          data_li[i].lock = tag_mem_pkt.data[data_width_p-2];
          mask_li[i].tag = {tag_width_p{way_decode[i]}};
          mask_li[i].valid = way_decode[i];
          mask_li[i].lock = way_decode[i];
        end
      end

      // set tag and valid bit for the cache line, chosen by index and way_id.
      e_tag_set_tag: begin
        w_li = 1'b1;
        for (integer i = 0 ; i < ways_p; i++) begin
          data_li[i].tag = tag_mem_pkt.tag;
          data_li[i].valid = 1'b1;
          data_li[i].lock = 1'b0;
          mask_li[i].tag = {tag_width_p{way_decode[i]}};
          mask_li[i].valid = way_decode[i];
          mask_li[i].lock = 1'b0;
        end
      end

      // set tag, valid bit, and lock bit for the chosen cache line.
      e_tag_set_tag_and_lock: begin
        w_li = 1'b1;
        for (integer i = 0 ; i < ways_p; i++) begin
          data_li[i].tag = tag_mem_pkt.tag;
          data_li[i].valid = 1'b1;
          data_li[i].lock = 1'b1;
          mask_li[i].tag = {tag_width_p{way_decode[i]}};
          mask_li[i].valid = way_decode[i];
          mask_li[i].lock = way_decode[i];
        end
      end

      // set valid bit to zero for the chosen line.
      // also unlocks the line.
      e_tag_invalidate: begin
        w_li = 1'b1;
        for (integer i = 0 ; i < ways_p; i++) begin
          data_li[i].tag = tag_mem_pkt.tag;
          data_li[i].valid = 1'b0;
          data_li[i].lock = 1'b0;
          mask_li[i].tag = {tag_width_p{1'b0}};
          mask_li[i].valid = way_decode[i];
          mask_li[i].lock = way_decode[i];
        end
      end

      // lock the chosen line. 
      e_tag_lock: begin
        w_li = 1'b1;
        for (integer i = 0 ; i < ways_p; i++) begin
          data_li[i].tag = tag_mem_pkt.tag;
          data_li[i].valid = 1'b0;
          data_li[i].lock = 1'b1;
          mask_li[i].tag = {tag_width_p{1'b0}};
          mask_li[i].valid = 1'b0;
          mask_li[i].lock = way_decode[i];
        end
      end

      // unlock the chosen line.
      e_tag_unlock: begin
        w_li = 1'b1;
        for (integer i = 0 ; i < ways_p; i++) begin
          data_li[i].tag = tag_mem_pkt.tag;
          data_li[i].valid = 1'b0;
          data_li[i].lock = 1'b0;
          mask_li[i].tag = {tag_width_p{1'b0}};
          mask_li[i].valid = 1'b0;
          mask_li[i].lock = way_decode[i];
        end
      end

      default: begin
        // this should never be used.
      end

    endcase
  end 

  // output logic
  //
  for (genvar i = 0; i < ways_p; i++) begin
    assign valid_o[i] = data_lo[i].valid;
    assign lock_o[i] = data_lo[i].lock;
    assign tag_o[i] = data_lo[i].tag;
  end

endmodule
