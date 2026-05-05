import { NextRequest, NextResponse } from "next/server";

const BACKEND_ESTIMATE_URL =
  process.env.SEENAD_BACKEND_ESTIMATE_URL ?? "http://localhost:8000/api/estimate-price";

export async function POST(req: NextRequest) {
  try {
    const payload = await req.json();
    const backendPayload = {
      brand: payload.brand,
      model_name: payload.model_name ?? payload.model,
      model_year: payload.model_year ?? payload.year,
      mileage: payload.mileage,
    };

    const response = await fetch(BACKEND_ESTIMATE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(backendPayload),
      cache: "no-store",
    });

    if (!response.ok) {
      const errorPayload = await response.json().catch(() => ({}));
      const backendMessage =
        errorPayload?.detail ?? errorPayload?.message ?? "Backend estimator is unavailable";
      return NextResponse.json(
        { message: backendMessage },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ message: "Invalid request" }, { status: 400 });
  }
}
