import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Seen Ad Calculator",
  description: "Professional pricing calculator for Seen Ad platform",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="fa" dir="rtl">
      <body>{children}</body>
    </html>
  );
}
