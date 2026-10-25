package main

import (
	"encoding/binary"
	"net"
	"testing"
)

func dnsQuery(name string, kind uint16) []byte {
	packet := []byte{0x12, 0x34, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0}
	start := 0
	for i := 0; i <= len(name); i++ {
		if i == len(name) || name[i] == '.' {
			packet = append(packet, byte(i-start))
			packet = append(packet, []byte(name[start:i])...)
			start = i + 1
		}
	}
	packet = append(packet, 0, byte(kind>>8), byte(kind), 0, 1)
	return packet
}
func TestLocalDNSAnswersAAndNoFalseAAAA(t *testing.T) {
	q := dnsQuery("Board.Example", 1)
	answer, local := localDNSAnswer(q, "board.example", net.ParseIP("192.0.2.10"))
	if !local || binary.BigEndian.Uint16(answer[6:8]) != 1 || net.IP(answer[len(answer)-4:]).String() != "192.0.2.10" {
		t.Fatalf("wrong A answer %x", answer)
	}
	if answer[0] != q[0] || answer[1] != q[1] {
		t.Fatal("transaction ID changed")
	}
	answer, local = localDNSAnswer(dnsQuery("board.example", 28), "board.example", net.ParseIP("192.0.2.10"))
	if !local || binary.BigEndian.Uint16(answer[6:8]) != 0 || answer[3]&15 != 0 {
		t.Fatal("AAAA must be NODATA")
	}
	if _, local = localDNSAnswer(dnsQuery("example.com", 1), "board.example", net.ParseIP("192.0.2.10")); local {
		t.Fatal("intercepted unrelated query")
	}
}
func TestMalformedDNSDoesNotPanic(t *testing.T) {
	q := dnsQuery("board.example", 1)
	for n := 0; n < len(q); n++ {
		if _, _, _, err := dnsQuestion(q[:n]); err == nil {
			t.Fatalf("accepted prefix of length %d", n)
		}
		localDNSAnswer(q[:n], "board.example", net.ParseIP("192.0.2.10"))
	}
	q[12] = 0xc0
	if _, _, _, err := dnsQuestion(q); err == nil {
		t.Fatal("accepted compressed query without parsing it")
	}
}
