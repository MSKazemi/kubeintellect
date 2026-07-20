import type { Metadata } from "next";
import { Geist_Mono } from "next/font/google";
import "./globals.css";
import "@xterm/xterm/css/xterm.css";

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "kube_q — AI Kubernetes client",
  description: "AI-powered Kubernetes assistant running in your browser",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${geistMono.variable} h-full overflow-hidden`}>
      <body className="h-full overflow-hidden">{children}</body>
    </html>
  );
}
