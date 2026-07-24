import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "BrandBlaze — One product. Every market.",
  description: "Generate identity-consistent product imagery at global scale with Genblaze and Backblaze B2.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
