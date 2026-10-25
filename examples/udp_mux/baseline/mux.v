// A combinational user-defined primitive, costed from its truth table.
primitive mux2(o, a, b, s);
  output o; input a, b, s;
  table
    0 ? 0 : 0;
    1 ? 0 : 1;
    ? 0 1 : 0;
    ? 1 1 : 1;
  endtable
endprimitive
module mux4w(input [3:0] a, b, input s, output [3:0] y);
  mux2 m0(y[0], a[0], b[0], s);
  mux2 m1(y[1], a[1], b[1], s);
  mux2 m2(y[2], a[2], b[2], s);
  mux2 m3(y[3], a[3], b[3], s);
endmodule
