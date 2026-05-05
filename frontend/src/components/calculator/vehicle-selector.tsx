"use client";

import { useMemo } from "react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { getBrandLabelFa } from "@/lib/brand-translation";
import type { ReferenceBrandOption, VehicleSelection } from "@/types/calculator";

export interface VehicleSelectorProps {
  brands: ReferenceBrandOption[];
  value: VehicleSelection;
  onChange: (next: VehicleSelection) => void;
  disabled?: boolean;
}

/** First available brand / model / trim for initial state (from reference-tree). */
export function defaultVehicleSelection(brands: ReferenceBrandOption[]): VehicleSelection {
  const b = brands[0];
  const m = b?.models[0];
  const trims = m?.trims ?? [];
  const t = trims[0];
  return {
    brand: b?.value ?? "",
    model: m?.value ?? "",
    carReferenceId: t?.car_reference_id ?? null,
  };
}

export function VehicleSelector({ brands, value, onChange, disabled }: VehicleSelectorProps) {
  const selectedBrand = useMemo(
    () => brands.find((x) => x.value === value.brand),
    [brands, value.brand]
  );
  const modelOptions = useMemo(() => selectedBrand?.models ?? [], [selectedBrand]);
  const selectedModel = useMemo(
    () => modelOptions.find((m) => m.value === value.model),
    [modelOptions, value.model]
  );
  const trimOptions = selectedModel?.trims ?? [];

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <label className="text-sm font-medium">برند</label>
        <Select
          value={value.brand}
          disabled={disabled || brands.length === 0}
          onValueChange={(newBrand) => {
            const node = brands.find((b) => b.value === newBrand);
            const firstModel = node?.models[0];
            const firstTrim = firstModel?.trims[0];
            onChange({
              brand: newBrand,
              model: firstModel?.value ?? "",
              carReferenceId: firstTrim?.car_reference_id ?? null,
            });
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="برند را انتخاب کنید" />
          </SelectTrigger>
          <SelectContent position="popper" sideOffset={8}>
            {brands.map((brand) => (
              <SelectItem key={brand.value} value={brand.value}>
                {getBrandLabelFa(brand.label || brand.value)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <label className="text-sm font-medium">مدل</label>
        <Select
          value={value.model}
          disabled={disabled || !value.brand || modelOptions.length === 0}
          onValueChange={(newModel) => {
            const m = modelOptions.find((x) => x.value === newModel);
            const firstTrim = m?.trims[0];
            onChange({
              ...value,
              model: newModel,
              carReferenceId: firstTrim?.car_reference_id ?? null,
            });
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="مدل را انتخاب کنید" />
          </SelectTrigger>
          <SelectContent position="popper" sideOffset={8}>
            {modelOptions.map((model) => (
              <SelectItem key={model.value} value={model.value}>
                {model.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2">
        <label className="text-sm font-medium">تریم</label>
        <Select
          value={value.carReferenceId != null ? String(value.carReferenceId) : ""}
          disabled={disabled || !value.model || trimOptions.length === 0}
          onValueChange={(trimIdStr) => {
            const id = Number.parseInt(trimIdStr, 10);
            if (Number.isNaN(id)) return;
            onChange({ ...value, carReferenceId: id });
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="تریم را انتخاب کنید" />
          </SelectTrigger>
          <SelectContent position="popper" sideOffset={8}>
            {trimOptions.map((trim) => (
              <SelectItem key={trim.car_reference_id} value={String(trim.car_reference_id)}>
                {trim.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
