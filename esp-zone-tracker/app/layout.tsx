import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ESP Zone Tracker",
  description: "BLE room positioning and safe-zone alerts for an ESP32-S3",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
