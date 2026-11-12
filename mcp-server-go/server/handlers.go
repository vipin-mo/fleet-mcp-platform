package server

import (
	"encoding/json"
	"fmt"
	"math"
	"mcp-server-go/database"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/redis/go-redis/v9"
)

// DeviceSetKey is the Redis set that lists every vehicle ID (written by the simulator).
const DeviceSetKey = "fleet:devices"

// Longest pause the agent may ask for between a fix and its re-check.
const maxWaitSeconds = 15.0

// How often send_ota_command retries when the vehicle changes between its checks and its write.
const maxWriteAttempts = 3

// Structural schemas for arguments passed by the LLM Orchestrator
type FleetSnapshotArgs struct {
	MinSpeed float64
	MinTemp  float64
}

type OTACommandArgs struct {
	DeviceID  string
	Parameter string
	Value     string
}

type HistoricalLogsArgs struct {
	DeviceID string
}

func getFloat(m map[string]interface{}, key string) float64 {
	if val, ok := m[key]; ok {
		if f, ok := val.(float64); ok {
			return f
		}
	}
	return 0.0
}

func getString(m map[string]interface{}, key string) string {
	if val, ok := m[key]; ok {
		if s, ok := val.(string); ok {
			return s
		}
	}
	return ""
}

// ageSeconds is how old a vehicle's last telemetry write is. It returns -1 when the vehicle has no
// timestamp, so the agent can tell a stopped feed from a healthy fleet.
func ageSeconds(updatedAt string) int64 {
	written, err := strconv.ParseInt(updatedAt, 10, 64)
	if err != nil {
		return -1
	}
	age := time.Now().Unix() - written
	if age < 0 {
		return 0
	}
	return age
}

func round2(x float64) float64 {
	return math.Round(x*100) / 100
}

// HandleGetFleetSnapshot returns the vehicles that match the speed and temperature filters.
// It reads the device ID set and fetches all hashes in one pipelined round trip (no KEYS scan).
func HandleGetFleetSnapshot(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	args := FleetSnapshotArgs{
		MinSpeed: getFloat(arguments, "min_speed"),
		MinTemp:  getFloat(arguments, "min_temp"),
	}

	matchingDevices := []map[string]interface{}{}

	deviceIDs, err := database.RedisClient.SMembers(database.Ctx, DeviceSetKey).Result()
	if err != nil {
		return nil, err
	}
	sort.Strings(deviceIDs)

	pipe := database.RedisClient.Pipeline()
	cmds := make([]*redis.MapStringStringCmd, len(deviceIDs))
	for i, id := range deviceIDs {
		cmds[i] = pipe.HGetAll(database.Ctx, "device:"+id)
	}
	if _, err := pipe.Exec(database.Ctx); err != nil && err != redis.Nil {
		return nil, err
	}

	for _, cmd := range cmds {
		data, err := cmd.Result()
		if err != nil || len(data) == 0 {
			continue
		}

		speed, errSpeed := strconv.ParseFloat(data["speed"], 64)
		temp, errTemp := strconv.ParseFloat(data["battery_temp"], 64)

		// Skip record processing if underlying fields contain invalid configurations
		if errSpeed != nil || errTemp != nil {
			continue
		}

		if speed >= args.MinSpeed && temp >= args.MinTemp {
			matchingDevices = append(matchingDevices, map[string]interface{}{
				"device_id":       data["device_id"],
				"speed":           speed,
				"battery_temp":    temp,
				"status":          data["cabin_status"],
				"power_parameter": data["power_parameter"],
				"speed_governor":  data["speed_governor"],
				"age_seconds":     ageSeconds(data["updated_at"]),
			})
		}
	}

	resultBytes, _ := json.Marshal(matchingDevices)
	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{
				Text: string(resultBytes),
			},
		},
	}, nil
}

// HandleScanFleet checks every vehicle on the server and returns only the ones that need action.
// The agent no longer decides which cars to look at, so a battery that is warming but still under
// some pre-filter cannot be skipped. One round trip reads all vehicle hashes and all histories.
func HandleScanFleet(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	deviceIDs, err := database.RedisClient.SMembers(database.Ctx, DeviceSetKey).Result()
	if err != nil {
		return nil, err
	}
	sort.Strings(deviceIDs)

	pipe := database.RedisClient.Pipeline()
	hashes := make([]*redis.MapStringStringCmd, len(deviceIDs))
	logs := make([]*redis.StringSliceCmd, len(deviceIDs))
	for i, id := range deviceIDs {
		hashes[i] = pipe.HGetAll(database.Ctx, "device:"+id)
		logs[i] = pipe.LRange(database.Ctx, "logs:"+id, 0, HistoryWindow-1)
	}
	if _, err := pipe.Exec(database.Ctx); err != nil && err != redis.Nil {
		return nil, err
	}

	readings := make([]DeviceReading, 0, len(deviceIDs))
	for i, id := range deviceIDs {
		data, err := hashes[i].Result()
		if err != nil || len(data) == 0 {
			// Listed in the set but the hash is gone (expired or wiped): treat as stale, never as healthy.
			readings = append(readings, DeviceReading{DeviceID: id, AgeSeconds: -1})
			continue
		}
		speed, _ := strconv.ParseFloat(data["speed"], 64)
		temp, _ := strconv.ParseFloat(data["battery_temp"], 64)
		history, _ := logs[i].Result()
		readings = append(readings, DeviceReading{
			DeviceID:       id,
			Speed:          speed,
			BatteryTemp:    temp,
			CabinStatus:    data["cabin_status"],
			PowerParameter: data["power_parameter"],
			SpeedGovernor:  data["speed_governor"],
			AgeSeconds:     ageSeconds(data["updated_at"]),
			Temps:          TempsFromLogs(history),
		})
	}

	resultBytes, _ := json.Marshal(BuildScanReport(readings))
	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{Text: string(resultBytes)},
		},
	}, nil
}

// HandleWaitForTelemetry pauses so the agent can let new readings arrive before it re-checks a fix.
func HandleWaitForTelemetry(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	seconds := getFloat(arguments, "seconds")
	if seconds < 1 {
		seconds = 1
	}
	if seconds > maxWaitSeconds {
		seconds = maxWaitSeconds
	}
	time.Sleep(time.Duration(seconds * float64(time.Second)))

	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{
				Text: fmt.Sprintf("Waited %.0f seconds. New telemetry has arrived.", seconds),
			},
		},
	}, nil
}

// HandleSendOTACommand alters dynamic fields inside the targeted fleet database container
func HandleSendOTACommand(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	args := OTACommandArgs{
		DeviceID:  getString(arguments, "device_id"),
		Parameter: getString(arguments, "parameter"),
		Value:     getString(arguments, "value"),
	}

	if err := ValidateOTACommand(args.DeviceID, args.Parameter, args.Value); err != nil {
		return &mcp.CallToolResult{
			IsError: true,
			Content: []mcp.Content{
				&mcp.TextContent{
					Text: fmt.Sprintf("Rejected by server-side validation: %v", err),
				},
			},
		}, nil
	}

	redisKey := fmt.Sprintf("device:%s", args.DeviceID)

	// The existence check, the freshness check and the write run as one optimistic transaction: WATCH the
	// vehicle, read it, then MULTI/EXEC the write. If the simulator rewrites the vehicle in between, EXEC
	// fails and the checks run again on the new data, so a write is never based on a reading that changed.
	for attempt := 0; attempt < maxWriteAttempts; attempt++ {
		var refusal *mcp.CallToolResult
		err := database.RedisClient.Watch(database.Ctx, func(tx *redis.Tx) error {
			exists, err := tx.Exists(database.Ctx, redisKey).Result()
			if err != nil {
				return err
			}
			if exists == 0 {
				refusal = textResult(false, fmt.Sprintf("Failure: Device %s not found in fleet registry map", args.DeviceID))
				return nil
			}
			updatedAt, err := tx.HGet(database.Ctx, redisKey, "updated_at").Result()
			if err != nil && err != redis.Nil {
				return err
			}
			// Never change settings on data we cannot trust.
			if err := CheckFreshForWrite(ageSeconds(updatedAt)); err != nil {
				refusal = textResult(true, fmt.Sprintf("Rejected: %v. Report STALE TELEMETRY instead of acting.", err))
				return nil
			}
			_, err = tx.TxPipelined(database.Ctx, func(pipe redis.Pipeliner) error {
				pipe.HSet(database.Ctx, redisKey, args.Parameter, args.Value)
				return nil
			})
			return err
		}, redisKey)

		if err == redis.TxFailedErr {
			continue // the vehicle changed while we checked it: check again
		}
		if err != nil {
			return nil, err
		}
		if refusal != nil {
			return refusal, nil
		}
		return textResult(false, fmt.Sprintf("Success: Over-The-Air command deployed to %s. Register '%s' updated to '%s'.",
			args.DeviceID, args.Parameter, args.Value)), nil
	}
	return textResult(true, fmt.Sprintf("Rejected: %s kept changing while the write was checked (%d attempts). Nothing was written; scan again.",
		args.DeviceID, maxWriteAttempts)), nil
}

func textResult(isError bool, text string) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		IsError: isError,
		Content: []mcp.Content{&mcp.TextContent{Text: text}},
	}
}

// HandleGetHistoricalLogs reads string arrays out of targeted list elements
func HandleGetHistoricalLogs(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	args := HistoricalLogsArgs{
		DeviceID: getString(arguments, "device_id"),
	}

	logKey := fmt.Sprintf("logs:%s", args.DeviceID)
	logs, err := database.RedisClient.LRange(database.Ctx, logKey, 0, 9).Result()
	if err != nil {
		return nil, err
	}

	var logSummary string
	for _, entry := range logs {
		logSummary += entry + "\n"
	}

	if logSummary == "" {
		logSummary = fmt.Sprintf("No historical timeline logs found for tracking key %s", args.DeviceID)
	}

	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{
				Text: logSummary,
			},
		},
	}, nil
}

// HandleStatAnomalyAnalysis compares a vehicle's newest battery temperatures with its own earlier
// baseline. The maths lives in AnalyzeTemperatureSeries so it can be unit tested without Redis.
func HandleStatAnomalyAnalysis(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	deviceID := getString(arguments, "device_id")
	logKey := fmt.Sprintf("logs:%s", deviceID)

	logs, err := database.RedisClient.LRange(database.Ctx, logKey, 0, HistoryWindow-1).Result()
	if err != nil {
		return nil, err
	}

	temps := TempsFromLogs(logs)

	result := AnalyzeTemperatureSeries(temps)

	responsePayload := map[string]interface{}{
		"device_id":             deviceID,
		"samples":               result.Samples,
		"current_temp":          result.LatestTemp,
		"baseline_mean_temp":    round2(result.BaselineMean),
		"baseline_std_dev":      round2(result.BaselineStdDev),
		"computed_z_score":      round2(result.ZScore),
		"recent_median_z_score": round2(result.RecentMedianZScore),
		"slope_per_reading":     round2(result.SlopePerReading),
		"classification":        result.Classification,
	}

	resultBytes, _ := json.Marshal(responsePayload)
	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{
				Text: string(resultBytes),
			},
		},
	}, nil
}

// HandleManualVectorSearch locates and streams manual data files across nested targets
func HandleManualVectorSearch(arguments map[string]interface{}) (*mcp.CallToolResult, error) {
	query := getString(arguments, "query")
	manualPath := "./agent-client-python/vehicle_fault_manual.md"

	if _, err := os.Stat(manualPath); os.IsNotExist(err) {
		manualPath = filepath.Join("..", "agent-client-python", "vehicle_fault_manual.md")
	}

	content, err := os.ReadFile(manualPath)
	if err != nil {
		return &mcp.CallToolResult{
			Content: []mcp.Content{
				&mcp.TextContent{
					Text: "System Error: Maintenance manual documentation store could not be located.",
				},
			},
		}, nil
	}

	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{
				Text: fmt.Sprintf("Extracted Relevant Technical Manual References for query '%s':\n\n%s", query, string(content)),
			},
		},
	}, nil
}
