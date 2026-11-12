package main

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"mcp-server-go/database"

	"github.com/alicebob/miniredis/v2"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/redis/go-redis/v9"
)

// The whole server as a client sees it: the tools registered in newServer, called over MCP, against Redis
// (in memory). The handler tests cover the edge cases; this checks that the pieces are wired together.
func connect(t *testing.T) (*mcp.ClientSession, *miniredis.Miniredis) {
	t.Helper()
	mr := miniredis.RunT(t)
	database.RedisClient = redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { database.RedisClient.Close() })

	ctx := context.Background()
	serverSide, clientSide := mcp.NewInMemoryTransports()
	if _, err := newServer().Connect(ctx, serverSide, nil); err != nil {
		t.Fatal(err)
	}
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil).Connect(ctx, clientSide, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { session.Close() })
	return session, mr
}

func call(t *testing.T, s *mcp.ClientSession, tool string, args map[string]any) (string, bool) {
	t.Helper()
	res, err := s.CallTool(context.Background(), &mcp.CallToolParams{Name: tool, Arguments: args})
	if err != nil {
		t.Fatalf("%s: %v", tool, err)
	}
	return res.Content[0].(*mcp.TextContent).Text, res.IsError
}

func seedFleet(mr *miniredis.Miniredis, cars int, cabin map[string]string) {
	now := strconv.FormatInt(time.Now().Unix(), 10)
	for i := 0; i < cars; i++ {
		id := fmt.Sprintf("car_%03d", i)
		status := cabin[id]
		if status == "" {
			status = "nominal"
		}
		mr.HSet("device:"+id, "device_id", id, "speed", "50", "battery_temp", "30.0", "cabin_status", status, "updated_at", now)
		mr.SAdd("fleet:devices", id)
		for j := 0; j < 20; j++ {
			mr.Lpush("logs:"+id, `{"battery_temp": 30.0, "speed": 50}`)
		}
	}
}

func TestMCPServerListsEveryTool(t *testing.T) {
	s, _ := connect(t)
	res, err := s.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, tool := range res.Tools {
		names = append(names, tool.Name)
	}
	sort.Strings(names)
	want := "analyze_anomalies_stats,get_fleet_snapshot,get_historical_logs,scan_fleet,search_maintenance_manual,send_ota_command,wait_for_telemetry"
	if got := strings.Join(names, ","); got != want {
		t.Fatalf("tools = %s", got)
	}
}

func TestMCPScanThenFixThenRescanOverTheProtocol(t *testing.T) {
	s, mr := connect(t)
	seedFleet(mr, 10, map[string]string{"car_004": "overheating_critical"})

	out, isErr := call(t, s, "scan_fleet", map[string]any{})
	var scan struct {
		FeedStatus string `json:"feed_status"`
		Flagged    []struct {
			DeviceID string   `json:"device_id"`
			Issues   []string `json:"issues"`
		} `json:"flagged"`
	}
	if isErr || json.Unmarshal([]byte(out), &scan) != nil {
		t.Fatalf("scan failed: %s", out)
	}
	if scan.FeedStatus != "ok" || len(scan.Flagged) != 1 || scan.Flagged[0].DeviceID != "car_004" {
		t.Fatalf("scan = %s", out)
	}

	out, isErr = call(t, s, "send_ota_command", map[string]any{"device_id": "car_004", "parameter": "speed_governor", "value": "50"})
	if isErr || !strings.HasPrefix(out, "Success") || mr.HGet("device:car_004", "speed_governor") != "50" {
		t.Fatalf("fix failed: %s", out)
	}

	// the simulator would clear the status a few ticks later; do it by hand and re-scan
	mr.HSet("device:car_004", "cabin_status", "nominal")
	out, _ = call(t, s, "scan_fleet", map[string]any{})
	if !strings.Contains(out, `"flagged":[]`) {
		t.Fatalf("re-scan still flags: %s", out)
	}
}

func TestMCPServerRefusesUnsafeWritesOnItsOwn(t *testing.T) {
	s, mr := connect(t)
	seedFleet(mr, 3, nil)
	for _, args := range []map[string]any{
		{"device_id": "car_001", "parameter": "power_parameter", "value": "sport_mode"},
		{"device_id": "car_001", "parameter": "speed_governor", "value": "120"},
		{"device_id": "car_001\n", "parameter": "speed_governor", "value": "50"},
		{"device_id": "car_001", "parameter": "battery_temp", "value": "20"},
	} {
		if out, isErr := call(t, s, "send_ota_command", args); !isErr {
			t.Fatalf("%v accepted: %s", args, out)
		}
	}
	if mr.HGet("device:car_001", "speed_governor") != "" || mr.HGet("device:car_001", "battery_temp") != "30.0" {
		t.Fatal("an unsafe write reached Redis")
	}
}

func TestMCPManualIsServedFromTheRepo(t *testing.T) {
	s, _ := connect(t)
	out, isErr := call(t, s, "search_maintenance_manual", map[string]any{"query": "battery"})
	if isErr || !strings.Contains(out, "ERR_BATT_THERMAL_RUNAWAY") || !strings.Contains(out, "ERR_CABIN_COOLANT_LEAK") {
		t.Fatalf("manual not returned: %.200s", out)
	}
}
