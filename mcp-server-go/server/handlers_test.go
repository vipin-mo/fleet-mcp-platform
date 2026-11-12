package server

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"mcp-server-go/database"

	"github.com/alicebob/miniredis/v2"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/redis/go-redis/v9"
)

// useMiniRedis points the handlers at an in-memory Redis for one test.
func useMiniRedis(t *testing.T) *miniredis.Miniredis {
	t.Helper()
	mr := miniredis.RunT(t)
	old := database.RedisClient
	database.RedisClient = redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() {
		database.RedisClient.Close()
		database.RedisClient = old
	})
	return mr
}

// putCar writes a vehicle the way the simulator does: hash, history (newest first) and the device set.
func putCar(t *testing.T, id string, ageSeconds int64, cabin string, temps []float64) {
	t.Helper()
	ctx := context.Background()
	c := database.RedisClient
	last := 30.0
	if len(temps) > 0 {
		last = temps[len(temps)-1]
	}
	fields := map[string]interface{}{"device_id": id, "speed": "50", "battery_temp": strconv.FormatFloat(last, 'f', 1, 64), "cabin_status": cabin}
	if ageSeconds >= 0 {
		fields["updated_at"] = strconv.FormatInt(time.Now().Unix()-ageSeconds, 10)
	}
	c.HSet(ctx, "device:"+id, fields)
	c.SAdd(ctx, DeviceSetKey, id)
	for _, temp := range temps { // LPUSH oldest first leaves the newest at the head, as the simulator does
		entry, _ := json.Marshal(map[string]float64{"battery_temp": temp, "speed": 50})
		c.LPush(ctx, "logs:"+id, entry)
	}
}

func text(r *mcp.CallToolResult) string {
	return r.Content[0].(*mcp.TextContent).Text
}

func send(t *testing.T, device, parameter, value string) *mcp.CallToolResult {
	t.Helper()
	r, err := HandleSendOTACommand(map[string]interface{}{"device_id": device, "parameter": parameter, "value": value})
	if err != nil {
		t.Fatalf("send_ota_command returned a Go error: %v", err)
	}
	return r
}

func register(mr *miniredis.Miniredis, device, field string) string {
	return mr.HGet("device:"+device, field)
}

func TestSendOTAWritesTheRegisterOnFreshData(t *testing.T) {
	mr := useMiniRedis(t)
	putCar(t, "car_001", 1, "nominal", nil)
	r := send(t, "car_001", "power_parameter", "eco_mode")
	if r.IsError || text(r) != "Success: Over-The-Air command deployed to car_001. Register 'power_parameter' updated to 'eco_mode'." {
		t.Fatalf("unexpected reply %q (error=%v)", text(r), r.IsError)
	}
	if got := register(mr, "car_001", "power_parameter"); got != "eco_mode" {
		t.Fatalf("register = %q, want eco_mode", got)
	}
}

func TestSendOTARefusesStaleOrUntimedDataAndWritesNothing(t *testing.T) {
	mr := useMiniRedis(t)
	putCar(t, "car_001", MaxTelemetryAgeSeconds+5, "nominal", nil)
	putCar(t, "car_002", -1, "nominal", nil) // no updated_at at all
	for _, id := range []string{"car_001", "car_002"} {
		r := send(t, id, "speed_governor", "50")
		if !r.IsError || !strings.Contains(text(r), "STALE TELEMETRY") {
			t.Fatalf("%s: want a stale refusal, got %q", id, text(r))
		}
		if got := register(mr, id, "speed_governor"); got != "" {
			t.Fatalf("%s: register written on stale data: %q", id, got)
		}
	}
}

func TestSendOTAUnknownDeviceAndInvalidPayloadWriteNothing(t *testing.T) {
	mr := useMiniRedis(t)
	putCar(t, "car_001", 1, "nominal", nil)
	if r := send(t, "car_099", "power_parameter", "eco_mode"); !strings.Contains(text(r), "not found") {
		t.Fatalf("unknown device: got %q", text(r))
	}
	if mr.Exists("device:car_099") {
		t.Fatal("a write to an unknown device created it")
	}
	for _, bad := range [][2]string{{"power_parameter", "sport_mode"}, {"speed_governor", "90"}, {"battery_temp", "20"}} {
		if r := send(t, "car_001", bad[0], bad[1]); !r.IsError {
			t.Fatalf("%v was accepted", bad)
		}
		if got := register(mr, "car_001", bad[0]); bad[0] != "battery_temp" && got != "" {
			t.Fatalf("%v was written", bad)
		}
	}
	if got := register(mr, "car_001", "battery_temp"); got != "30.0" {
		t.Fatalf("protected register changed to %q", got)
	}
}

// changeDuringFirstWrite edits the vehicle through a second client just before the first MULTI/EXEC, i.e.
// after send_ota_command has checked it and before it writes.
type changeDuringFirstWrite struct {
	once   sync.Once
	other  *redis.Client
	key    string
	values map[string]interface{}
}

func (h *changeDuringFirstWrite) DialHook(next redis.DialHook) redis.DialHook          { return next }
func (h *changeDuringFirstWrite) ProcessHook(next redis.ProcessHook) redis.ProcessHook { return next }
func (h *changeDuringFirstWrite) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, cmds []redis.Cmder) error {
		h.once.Do(func() { h.other.HSet(ctx, h.key, h.values) })
		return next(ctx, cmds)
	}
}

func raceOn(t *testing.T, mr *miniredis.Miniredis, key string, values map[string]interface{}) {
	t.Helper()
	other := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { other.Close() })
	database.RedisClient.AddHook(&changeDuringFirstWrite{other: other, key: key, values: values})
}

func TestSendOTADoesNotWriteWhenTheDataGoesStaleBetweenCheckAndWrite(t *testing.T) {
	mr := useMiniRedis(t)
	putCar(t, "car_001", 1, "nominal", nil)
	old := strconv.FormatInt(time.Now().Unix()-MaxTelemetryAgeSeconds-10, 10)
	raceOn(t, mr, "device:car_001", map[string]interface{}{"updated_at": old})

	r := send(t, "car_001", "power_parameter", "eco_mode")
	if !r.IsError || !strings.Contains(text(r), "STALE TELEMETRY") {
		t.Fatalf("want a stale refusal after the race, got %q", text(r))
	}
	if got := register(mr, "car_001", "power_parameter"); got != "" {
		t.Fatalf("wrote %q on data that went stale before the write", got)
	}
}

func TestSendOTARetriesWhenTheVehicleChangesButStaysFresh(t *testing.T) {
	mr := useMiniRedis(t)
	putCar(t, "car_001", 1, "nominal", nil)
	raceOn(t, mr, "device:car_001", map[string]interface{}{"speed": "51"}) // a normal telemetry tick

	r := send(t, "car_001", "speed_governor", "30")
	if r.IsError || !strings.HasPrefix(text(r), "Success") {
		t.Fatalf("want success on the retry, got %q", text(r))
	}
	if got := register(mr, "car_001", "speed_governor"); got != "30" {
		t.Fatalf("register = %q, want 30", got)
	}
}

func TestScanFleetReadsRedisAndFlagsOnlyFreshFaults(t *testing.T) {
	useMiniRedis(t)
	flat := flatSeries(60, 30)
	ramp := join(flatSeries(45, 30), rampTail(30, 0.6, 15))
	for i := 1; i <= 8; i++ {
		putCar(t, fmt.Sprintf("car_%03d", i), 1, "nominal", flat)
	}
	putCar(t, "car_009", 1, "nominal", ramp)                        // battery fault
	putCar(t, "car_010", 1, "coolant_leak", flat)                   // cabin fault
	putCar(t, "car_011", MaxTelemetryAgeSeconds+5, "nominal", ramp) // stale: never analysed

	r, err := HandleScanFleet(nil)
	if err != nil {
		t.Fatal(err)
	}
	var report ScanReport
	if err := json.Unmarshal([]byte(text(r)), &report); err != nil {
		t.Fatal(err)
	}
	if report.FeedStatus != FeedOK || report.FleetSize != 11 || report.Checked != 10 {
		t.Fatalf("status=%s size=%d checked=%d", report.FeedStatus, report.FleetSize, report.Checked)
	}
	if len(report.Flagged) != 2 || findByID(report, "car_009") == nil || findByID(report, "car_010") == nil {
		t.Fatalf("flagged = %+v, want car_009 and car_010", report.Flagged)
	}
	if len(report.StaleDevices) != 1 || report.StaleDevices[0] != "car_011" {
		t.Fatalf("stale = %v, want [car_011]", report.StaleDevices)
	}
}

func TestScanFleetTreatsAListedCarWithoutAHashAsStale(t *testing.T) {
	useMiniRedis(t)
	database.RedisClient.SAdd(context.Background(), DeviceSetKey, "car_001")
	r, _ := HandleScanFleet(nil)
	var report ScanReport
	json.Unmarshal([]byte(text(r)), &report)
	if report.FeedStatus != FeedStale || len(report.Flagged) != 0 {
		t.Fatalf("a car with no data must make the feed stale, got %+v", report)
	}
}
