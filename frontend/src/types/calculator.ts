export interface ReferenceTrimOption {
  label: string;
  car_reference_id: number;
}

export interface ReferenceModelOption {
  label: string;
  value: string;
  trims: ReferenceTrimOption[];
}

export interface ReferenceBrandOption {
  label: string;
  value: string;
  models: ReferenceModelOption[];
}

/** UI state for brand → model → trim (trim = car_reference row). */
export interface VehicleSelection {
  brand: string;
  model: string;
  carReferenceId: number | null;
}

export interface EstimateRequest {
  brand?: string;
  model?: string;
  year: number;
  mileage: number;
  car_reference_id: number;
}

export interface PricePercentiles {
  p10?: number | null;
  p25?: number | null;
  median?: number | null;
  p75?: number | null;
  p90?: number | null;
}

export interface EstimateResponse {
  estimated_price: number;
  min_price: number;
  max_price: number;
  currency: string;
  sample_size?: number;
  strategy?: string;
  percentiles?: PricePercentiles | null;
  confidence_level?: string;
  confidence_label?: string;
}

export interface AvailableCarsResponse {
  brands: ReferenceBrandOption[];
}
