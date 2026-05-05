import { NextResponse } from "next/server";

const BACKEND_BASE_URL = process.env.SEENAD_BACKEND_URL ?? "http://backend:8000";
const BACKEND_AVAILABLE_CARS_URL =
  process.env.SEENAD_BACKEND_AVAILABLE_CARS_URL ?? `${BACKEND_BASE_URL}/api/v1/vehicles/reference-tree`;

export async function GET() {
  try {
    const response = await fetch(BACKEND_AVAILABLE_CARS_URL, {
      method: "GET",
      cache: "no-store",
    });
    if (!response.ok) {
      return NextResponse.json({ message: "Backend available cars API is unavailable" }, { status: response.status });
    }
    const data = await response.json();
    return NextResponse.json(data);
  } catch {
    return NextResponse.json({ message: "Invalid request" }, { status: 400 });
  }
}
