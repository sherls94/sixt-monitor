// Shapes of the JSONB columns written by price_monitor.py (see make_result() /
// _format_results_section() / _write_json_results() in the Python backend).

export interface ProviderResult {
  provider: string;
  car_class: string;
  model: string;
  price: number | null;
  saving: number | null;
  source: string;
  status: "ok" | "error" | "na";
  booking_url: string | null;
  image_url: string | null;
}

export interface NearbyRow {
  location_name: string;
  airport_code: string | null;
  location_key: string;
  distance_miles: number | null;
  cab_fare: number | null;
  best_price: number | null;
  best_provider: string | null;
  net_saving: number | null;
  is_deal: boolean;
}

export interface ResultsSummary {
  best_direct_provider: string | null;
  best_direct_price: number | null;
  best_direct_saving: number | null;
}
