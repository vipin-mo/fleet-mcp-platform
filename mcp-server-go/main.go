package main

import (
	"context"
	"log"
	"mcp-server-go/database"
	"mcp-server-go/server"
	"os"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// Input parameter definitions matching orchestrator payload keys
type FleetSnapshotInput struct {
	MinSpeed float64 `json:"min_speed"`
	MinTemp  float64 `json:"min_temp"`
}

type SendOTACommandInput struct {
	DeviceID  string `json:"device_id"`
	Parameter string `json:"parameter"`
	Value     string `json:"value"`
}

type HistoricalLogsInput struct {
	DeviceID string `json:"device_id"`
}

type StatAnomalyInput struct {
	DeviceID string `json:"device_id"`
}

type MaintenanceManualInput struct {
	Query string `json:"query"`
}

type ScanFleetInput struct{}

type WaitInput struct {
	Seconds float64 `json:"seconds"`
}

// Explicit structural outputs for type-safe SDK callback registration
type FleetSnapshotOutput struct{}
type SendOTACommandOutput struct{}
type HistoricalLogsOutput struct{}
type StatAnomalyOutput struct{}
type MaintenanceManualOutput struct{}
type WaitOutput struct{}
type ScanFleetOutput struct{}

// helperToSchema builds a single-property schema to prevent discovery parsing failures
func helperToSchema(propName string, propType string, description string) map[string]interface{} {
	return map[string]interface{}{
		"type": "object",
		"properties": map[string]interface{}{
			propName: map[string]interface{}{
				"type":        propType,
				"description": description,
			},
		},
		"required": []string{propName},
	}
}

func main() {
	// 1. Initialize persistent connection pool to Redis container
	if err := database.InitRedis(); err != nil {
		log.Fatalf("Critical error connecting to local Redis instance: %v", err)
	}

	ctx := context.Background()
	s := newServer()

	// 2. Launch standard I/O communication server loop execution block
	log.Println("Starting Go MCP server via Stdio transport pipeline...")
	if err := s.Run(ctx, &mcp.StdioTransport{}); err != nil {
		log.Printf("Server execution context terminated unexpectedly: %v", err)
		os.Exit(1)
	}
}

// newServer builds the MCP server with every tool registered. main runs it over stdio; the tests run the
// same server over an in-memory transport.
func newServer() *mcp.Server {
	// Initialize the Base MCP Server framework structure
	s := mcp.NewServer(
		&mcp.Implementation{
			Name:    "mcp-server-go",
			Version: "1.0.0",
		},
		nil,
	)

	// 3. Register 'get_fleet_snapshot'
	mcp.AddTool(s, &mcp.Tool{
		Name:        "get_fleet_snapshot",
		Description: "Queries active vehicle fleet state metrics filtered by minimum speed and battery temperature thresholds.",
		InputSchema: map[string]interface{}{
			"type": "object",
			"properties": map[string]interface{}{
				"min_speed": map[string]interface{}{"type": "number", "description": "Minimum vehicle speed filter boundary"},
				"min_temp":  map[string]interface{}{"type": "number", "description": "Minimum battery temperature filter boundary"},
			},
			"required": []string{"min_speed", "min_temp"},
		},
	}, func(ctx context.Context, req *mcp.CallToolRequest, input FleetSnapshotInput) (*mcp.CallToolResult, FleetSnapshotOutput, error) {
		argsMap := map[string]interface{}{
			"min_speed": input.MinSpeed,
			"min_temp":  input.MinTemp,
		}
		res, err := server.HandleGetFleetSnapshot(argsMap)
		return res, FleetSnapshotOutput{}, err
	})

	// 4. Register 'send_ota_command'
	mcp.AddTool(s, &mcp.Tool{
		Name:        "send_ota_command",
		Description: "Deploys targeted configuration updates directly to a specific vehicle hardware parameter register.",
		InputSchema: map[string]interface{}{
			"type": "object",
			"properties": map[string]interface{}{
				"device_id": map[string]interface{}{"type": "string", "description": "Target unique vehicle identifier (e.g. car_012)"},
				"parameter": map[string]interface{}{"type": "string", "description": "The target state parameter register to mutate ('power_parameter' or 'speed_governor')"},
				"value":     map[string]interface{}{"type": "string", "description": "The configuration target value string (e.g., 'eco_mode' or '50')"},
			},
			"required": []string{"device_id", "parameter", "value"},
		},
	}, func(ctx context.Context, req *mcp.CallToolRequest, input SendOTACommandInput) (*mcp.CallToolResult, SendOTACommandOutput, error) {
		argsMap := map[string]interface{}{
			"device_id": input.DeviceID,
			"parameter": input.Parameter,
			"value":     input.Value,
		}
		res, err := server.HandleSendOTACommand(argsMap)
		return res, SendOTACommandOutput{}, err
	})

	// 5. Register 'get_historical_logs'
	mcp.AddTool(s, &mcp.Tool{
		Name:        "get_historical_logs",
		Description: "Retrieves a trailing rolling sequence of telemetry history logs for a single vehicle.",
		InputSchema: helperToSchema("device_id", "string", "Target unique vehicle identifier"),
	}, func(ctx context.Context, req *mcp.CallToolRequest, input HistoricalLogsInput) (*mcp.CallToolResult, HistoricalLogsOutput, error) {
		argsMap := map[string]interface{}{"device_id": input.DeviceID}
		res, err := server.HandleGetHistoricalLogs(argsMap)
		return res, HistoricalLogsOutput{}, err
	})

	// 6. Register 'analyze_anomalies_stats'
	mcp.AddTool(s, &mcp.Tool{
		Name: "analyze_anomalies_stats",
		Description: "Compares a vehicle's newest battery temperature readings with its own earlier baseline " +
			"(z-score and trend). Returns a classification: sustained_anomaly, rising_trend, transient_spike, " +
			"nominal or insufficient_data.",
		InputSchema: helperToSchema("device_id", "string", "Target unique vehicle identifier for anomaly verification"),
	}, func(ctx context.Context, req *mcp.CallToolRequest, input StatAnomalyInput) (*mcp.CallToolResult, StatAnomalyOutput, error) {
		argsMap := map[string]interface{}{"device_id": input.DeviceID}
		res, err := server.HandleStatAnomalyAnalysis(argsMap)
		return res, StatAnomalyOutput{}, err
	})

	// 7. Register 'search_maintenance_manual'
	mcp.AddTool(s, &mcp.Tool{
		Name:        "search_maintenance_manual",
		Description: "Queries enterprise technical documentation documentation to isolate operational fault configurations.",
		InputSchema: helperToSchema("query", "string", "Target telemetry anomaly or fault key search terms"),
	}, func(ctx context.Context, req *mcp.CallToolRequest, input MaintenanceManualInput) (*mcp.CallToolResult, MaintenanceManualOutput, error) {
		argsMap := map[string]interface{}{"query": input.Query}
		res, err := server.HandleManualVectorSearch(argsMap)
		return res, MaintenanceManualOutput{}, err
	})

	// 8. Register 'scan_fleet'
	mcp.AddTool(s, &mcp.Tool{
		Name: "scan_fleet",
		Description: "Checks EVERY vehicle on the server (battery trend, cabin status, data freshness) and returns only " +
			"the vehicles that need action, plus a feed_status of ok, stale or empty. Call this first and again after " +
			"each fix. If feed_status is not ok, the telemetry feed is down: report it and take no action.",
		InputSchema: map[string]interface{}{
			"type":       "object",
			"properties": map[string]interface{}{},
		},
	}, func(ctx context.Context, req *mcp.CallToolRequest, input ScanFleetInput) (*mcp.CallToolResult, ScanFleetOutput, error) {
		res, err := server.HandleScanFleet(map[string]interface{}{})
		return res, ScanFleetOutput{}, err
	})

	// 9. Register 'wait_for_telemetry'
	mcp.AddTool(s, &mcp.Tool{
		Name:        "wait_for_telemetry",
		Description: "Pauses for a few seconds so new telemetry can arrive. Use it after applying a fix, before re-checking the vehicle. Maximum 15 seconds.",
		InputSchema: helperToSchema("seconds", "number", "How many seconds to wait (1 to 15)"),
	}, func(ctx context.Context, req *mcp.CallToolRequest, input WaitInput) (*mcp.CallToolResult, WaitOutput, error) {
		argsMap := map[string]interface{}{"seconds": input.Seconds}
		res, err := server.HandleWaitForTelemetry(argsMap)
		return res, WaitOutput{}, err
	})

	return s
}
