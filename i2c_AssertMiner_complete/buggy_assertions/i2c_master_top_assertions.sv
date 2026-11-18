module i2c_master_top_assertions (
    input logic wb_clk_i,
    input logic wb_rst_i,
    input logic rst_i,
    input logic irq_flag,
    input logic ien,
    input logic wb_inta_o
);

  a_interrupt_request_generation: assert property (
    @(posedge wb_clk_i) disable iff (!rst_i || wb_rst_i)
    (irq_flag && ien) |-> ##1 (wb_inta_o == 1'b1)
  );

  a_interrupt_enable_preserves_flag_scope: assert property (
    @(posedge wb_clk_i) disable iff (!rst_i || wb_rst_i)
    (irq_flag && ien) |-> ##1 (irq_flag === irq_flag)
  );

  a_interrupt_route_observation: assert property (
    @(posedge wb_clk_i) disable iff (!rst_i || wb_rst_i)
    (irq_flag && ien) |-> aq_missing_interrupt_monitor.route_seen
  );

  a_enabled_interrupt_stays_low: assert property (
    @(posedge wb_clk_i) disable iff (!rst_i || wb_rst_i)
    (irq_flag && ien) |-> ##1 (wb_inta_o == 1'b0)
  );

endmodule
