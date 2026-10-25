package main

// Small LAN-only DNS proxy. Only the configured hostname is authoritative;
// other ordinary queries are forwarded byte-for-byte to explicit upstreams.
import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"log"
	"net"
	"strings"
	"sync/atomic"
	"time"
)

var dnsReady atomic.Bool

func dnsQuestion(packet []byte) (string, uint16, int, error) {
	if len(packet) < 12 || binary.BigEndian.Uint16(packet[4:6]) != 1 || packet[2]&0xf8 != 0 {
		return "", 0, 0, errors.New("expected one standard DNS question")
	}
	labels := []string{}
	position := 12
	for {
		if position >= len(packet) {
			return "", 0, 0, errors.New("incomplete name")
		}
		size := int(packet[position])
		position++
		if size == 0 {
			break
		}
		if size > 63 || position+size > len(packet) || position+size > 267 {
			return "", 0, 0, errors.New("invalid query label")
		}
		labels = append(labels, string(packet[position:position+size]))
		position += size
	}
	if position+4 > len(packet) || binary.BigEndian.Uint16(packet[position+2:position+4]) != 1 {
		return "", 0, 0, errors.New("incomplete or non-IN question")
	}
	return strings.ToLower(strings.Join(labels, ".")), binary.BigEndian.Uint16(packet[position : position+2]), position + 4, nil
}
func dnsError(query []byte, code byte) []byte {
	response := make([]byte, 12)
	if len(query) >= 2 {
		copy(response[:2], query[:2])
	}
	response[2] = 0x80
	response[3] = 0x80 | code
	return response
}
func localDNSAnswer(query []byte, hostname string, ip net.IP) ([]byte, bool) {
	name, kind, end, err := dnsQuestion(query)
	if err != nil {
		return dnsError(query, 1), true
	}
	if name != strings.ToLower(strings.TrimSuffix(hostname, ".")) {
		return nil, false
	}
	response := append([]byte(nil), query[:end]...)
	response[2] = 0x84 | (query[2] & 1)
	response[3] = 0x80
	for i := 6; i < 12; i++ {
		response[i] = 0
	}
	// AAAA and other record types get authoritative NODATA, avoiding false IPv6.
	if kind == 1 {
		ipv4 := ip.To4()
		if ipv4 == nil {
			return dnsError(query, 2), true
		}
		response[7] = 1
		answer := []byte{0xc0, 0x0c, 0, 1, 0, 1, 0, 0, 0, 60, 0, 4}
		response = append(response, answer...)
		response = append(response, ipv4...)
	}
	return response, true
}
func forwardDNS(query []byte, tcp bool, upstreams []string) []byte {
	for _, upstream := range upstreams {
		network := "udp"
		if tcp {
			network = "tcp"
		}
		conn, err := net.DialTimeout(network, upstream, 2*time.Second)
		if err != nil {
			continue
		}
		conn.SetDeadline(time.Now().Add(2 * time.Second))
		var response []byte
		if tcp {
			frame := make([]byte, 2+len(query))
			binary.BigEndian.PutUint16(frame, uint16(len(query)))
			copy(frame[2:], query)
			_, err = conn.Write(frame)
			if err == nil {
				length := make([]byte, 2)
				_, err = io.ReadFull(conn, length)
				if err == nil {
					response = make([]byte, int(binary.BigEndian.Uint16(length)))
					_, err = io.ReadFull(conn, response)
				}
			}
		} else {
			_, err = conn.Write(query)
			if err == nil {
				response = make([]byte, 65535)
				var count int
				count, err = conn.Read(response)
				response = response[:count]
			}
		}
		conn.Close()
		if err == nil && len(response) >= 12 && len(query) >= 2 && response[0] == query[0] && response[1] == query[1] && response[2]&0x80 != 0 {
			return response
		}
	}
	return dnsError(query, 2)
}
func dnsReply(query []byte, tcp bool, c config) []byte {
	if len(query) > 65535 {
		return dnsError(query, 1)
	}
	if answer, local := localDNSAnswer(query, c.Hostname, net.ParseIP(c.DNSIP)); local {
		return answer
	}
	return forwardDNS(query, tcp, c.DNSUpstreams)
}
func startDNS(ctx context.Context, c config, failures chan<- error) error {
	_, allowed, err := net.ParseCIDR(c.DNSCIDR)
	if err != nil {
		return err
	}
	udp, err := net.ListenPacket("udp4", c.DNSListen)
	if err != nil {
		return err
	}
	tcp, err := net.Listen("tcp4", c.DNSListen)
	if err != nil {
		udp.Close()
		return err
	}
	go func() { <-ctx.Done(); udp.Close(); tcp.Close() }()
	slots := make(chan struct{}, 64)
	go func() {
		for {
			buffer := make([]byte, 65535)
			count, from, err := udp.ReadFrom(buffer)
			if err != nil {
				select {
				case failures <- err:
				default:
				}
				return
			}
			if !allowed.Contains(from.(*net.UDPAddr).IP) {
				continue
			}
			select {
			case slots <- struct{}{}:
				go func(query []byte, from net.Addr) {
					defer func() { <-slots }()
					udp.WriteTo(dnsReply(query, false, c), from)
				}(buffer[:count], from)
			default:
			}
		}
	}()
	go func() {
		for {
			conn, err := tcp.Accept()
			if err != nil {
				select {
				case failures <- err:
				default:
				}
				return
			}
			if !allowed.Contains(conn.RemoteAddr().(*net.TCPAddr).IP) {
				conn.Close()
				continue
			}
			select {
			case slots <- struct{}{}:
				go func() {
					defer func() { conn.Close(); <-slots }()
					conn.SetDeadline(time.Now().Add(8 * time.Second))
					length := make([]byte, 2)
					if _, err := io.ReadFull(conn, length); err != nil {
						return
					}
					query := make([]byte, int(binary.BigEndian.Uint16(length)))
					if _, err := io.ReadFull(conn, query); err != nil {
						return
					}
					answer := dnsReply(query, true, c)
					frame := make([]byte, 2+len(answer))
					binary.BigEndian.PutUint16(frame, uint16(len(answer)))
					copy(frame[2:], answer)
					conn.Write(frame)
				}()
			default:
				conn.Close()
			}
		}
	}()
	return nil
}
func keepDNS(ctx context.Context, c config) {
	if c.DNSListen == "" {
		return
	}
	for {
		child, cancel := context.WithCancel(ctx)
		failures := make(chan error, 2)
		if err := startDNS(child, c, failures); err == nil {
			dnsReady.Store(true)
			log.Printf("LAN DNS active at %s", c.DNSListen)
			select {
			case <-ctx.Done():
				cancel()
				dnsReady.Store(false)
				return
			case err := <-failures:
				log.Printf("LAN DNS restarting: %v", err)
			}
		} else {
			log.Printf("LAN DNS waiting for network: %v", err)
		}
		cancel()
		dnsReady.Store(false)
		select {
		case <-ctx.Done():
			return
		case <-time.After(5 * time.Second):
		}
	}
}
