"use client";

import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { defaultVehicleSelection, VehicleSelector } from "@/components/calculator/vehicle-selector";
import { AvailableCarsResponse, EstimateResponse, VehicleSelection } from "@/types/calculator";

function formatPrice(value: number) {
  return new Intl.NumberFormat("fa-IR").format(value);
}

function formatCount(value: number) {
  return new Intl.NumberFormat("fa-IR").format(value);
}

export default function CalculatorForm() {
  const [vehicle, setVehicle] = useState<VehicleSelection>({
    brand: "",
    model: "",
    carReferenceId: null,
  });
  const [year, setYear] = useState(1401);
  const [mileage, setMileage] = useState(85000);
  const [availableCars, setAvailableCars] = useState<AvailableCarsResponse["brands"]>([]);
  const [isCarsLoading, setIsCarsLoading] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState<EstimateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;
    async function loadAvailableCars() {
      setIsCarsLoading(true);
      try {
        const response = await fetch("/api/v1/vehicles/reference-tree", { cache: "no-store" });
        if (!response.ok) {
          throw new Error("خطا در دریافت لیست خودروها");
        }
        const data = (await response.json()) as AvailableCarsResponse;
        console.log("Vehicle reference tree response:", data);
        if (!isMounted) return;
        const cars = data?.brands ?? [];
        setAvailableCars(cars);
        setVehicle((prev) => {
          if (prev.brand && prev.model && prev.carReferenceId != null) return prev;
          return defaultVehicleSelection(cars);
        });
      } catch (err) {
        if (!isMounted) return;
        setError(err instanceof Error ? err.message : "خطای ناشناخته");
      } finally {
        if (isMounted) setIsCarsLoading(false);
      }
    }
    void loadAvailableCars();
    return () => {
      isMounted = false;
    };
  }, []);

  const canSubmit = useMemo(() => {
    return Boolean(
      vehicle.brand &&
        vehicle.model &&
        vehicle.carReferenceId != null &&
        year > 1370 &&
        mileage >= 0 &&
        !isCarsLoading
    );
  }, [vehicle.brand, vehicle.model, vehicle.carReferenceId, year, mileage, isCarsLoading]);

  useEffect(() => {
    setResult(null);
    setError(null);
  }, [vehicle.brand, vehicle.model, vehicle.carReferenceId, year, mileage]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    setIsLoading(true);

    try {
      const requestPayload = {
        brand: vehicle.brand,
        model: vehicle.model,
        car_reference_id: vehicle.carReferenceId!,
        year,
        mileage,
      };
      console.log("Estimate request payload:", requestPayload);
      const response = await fetch("/api/estimate-price", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestPayload),
      });

      if (!response.ok) {
        const errorPayload = (await response.json().catch(() => ({}))) as {
          message?: string;
          detail?: string | string[];
        };
        const detail =
          typeof errorPayload.detail === "string"
            ? errorPayload.detail
            : Array.isArray(errorPayload.detail)
              ? errorPayload.detail.join(", ")
              : undefined;
        throw new Error(errorPayload.message || detail || "خطا در دریافت قیمت تخمینی");
      }

      const data = (await response.json()) as EstimateResponse;
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطای ناشناخته");
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-10">
      <div className="mb-8 flex items-center gap-3">
        <Badge className="bg-seenad-green text-white hover:bg-seenad-green-light">Seen Ad</Badge>
        <h1 className="text-3xl font-bold text-seenad-navy">Seen Ad Calculator</h1>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="border-seenad-green/25">
          <CardHeader>
            <CardTitle className="text-seenad-navy">مشخصات خودرو</CardTitle>
            <CardDescription>اطلاعات خودرو را وارد کنید تا بازه قیمتی تخمینی محاسبه شود.</CardDescription>
          </CardHeader>
          <CardContent>
            <form className="space-y-4" onSubmit={onSubmit}>
              <VehicleSelector
                brands={availableCars}
                value={vehicle}
                onChange={setVehicle}
                disabled={isCarsLoading}
              />

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-2">
                  <label className="text-sm font-medium">سال تولید</label>
                  <Input
                    type="number"
                    value={year}
                    onChange={(e) => setYear(Number(e.target.value))}
                  />
                </div>
                <div className="space-y-2">
                  <label className="text-sm font-medium">کارکرد (کیلومتر)</label>
                  <Input
                    type="number"
                    value={mileage}
                    onChange={(e) => setMileage(Number(e.target.value))}
                  />
                </div>
              </div>

              <Button
                type="submit"
                disabled={!canSubmit || isLoading}
                className="w-full bg-seenad-green text-white hover:bg-seenad-green-light"
              >
                {isLoading ? "در حال محاسبه..." : "محاسبه قیمت"}
              </Button>
            </form>
          </CardContent>
        </Card>

        <Card className="border-seenad-green/25 bg-seenad-mint">
          <CardHeader>
            <CardTitle className="text-seenad-navy">نتیجه تخمین قیمت</CardTitle>
            <CardDescription>
              بازه قیمت از جدول listings_clean (دیوار و باما، نگاشت‌شده به car_reference) با صدک‌های آماری محاسبه می‌شود.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {isLoading && (
              <div className="space-y-3">
                <Skeleton className="h-8 w-44" />
                <Skeleton className="h-24 w-full" />
              </div>
            )}

            {error && <p className="rounded-md bg-red-50 p-3 text-sm text-red-600">{error}</p>}

            {!isLoading && result && (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>شاخص</TableHead>
                    <TableHead>مقدار</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow>
                    <TableCell>قیمت تخمینی</TableCell>
                    <TableCell>{formatPrice(result.estimated_price)} تومان</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell>حداقل بازه</TableCell>
                    <TableCell>{formatPrice(result.min_price)} تومان</TableCell>
                  </TableRow>
                  <TableRow>
                    <TableCell>حداکثر بازه</TableCell>
                    <TableCell>{formatPrice(result.max_price)} تومان</TableCell>
                  </TableRow>
                  {result.percentiles?.p10 != null && (
                    <TableRow>
                      <TableCell>صدک ۱۰٪ (P10)</TableCell>
                      <TableCell>{formatPrice(result.percentiles.p10)} تومان</TableCell>
                    </TableRow>
                  )}
                  {result.percentiles?.p25 != null && (
                    <TableRow>
                      <TableCell>صدک ۲۵٪ (P25)</TableCell>
                      <TableCell>{formatPrice(result.percentiles.p25)} تومان</TableCell>
                    </TableRow>
                  )}
                  {result.percentiles?.median != null && (
                    <TableRow>
                      <TableCell>میانه (P50)</TableCell>
                      <TableCell>{formatPrice(result.percentiles.median)} تومان</TableCell>
                    </TableRow>
                  )}
                  {result.percentiles?.p75 != null && (
                    <TableRow>
                      <TableCell>صدک ۷۵٪ (P75)</TableCell>
                      <TableCell>{formatPrice(result.percentiles.p75)} تومان</TableCell>
                    </TableRow>
                  )}
                  {result.percentiles?.p90 != null && (
                    <TableRow>
                      <TableCell>صدک ۹۰٪ (P90)</TableCell>
                      <TableCell>{formatPrice(result.percentiles.p90)} تومان</TableCell>
                    </TableRow>
                  )}
                  {result.confidence_label && (
                    <TableRow>
                      <TableCell>سطح اطمینان</TableCell>
                      <TableCell>
                        {result.confidence_label}
                        {result.confidence_level ? (
                          <span className="mr-2 text-xs text-muted-foreground">({result.confidence_level})</span>
                        ) : null}
                      </TableCell>
                    </TableRow>
                  )}
                  <TableRow>
                    <TableCell>تعداد آگهی‌های بررسی‌شده</TableCell>
                    <TableCell>{formatCount(result.sample_size ?? 0)} آگهی</TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            )}

            {!isLoading && !result && !error && (
              <p className="text-sm text-muted-foreground">برای دیدن نتیجه، فرم را تکمیل و دکمه محاسبه را بزنید.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
