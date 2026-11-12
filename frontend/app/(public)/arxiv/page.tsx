import { Metadata } from 'next';
import ArxivManagement from '@/components/arxiv/ArxivManagement';

export const metadata: Metadata = {
  title: 'arXiv | NOUS',
  description: 'Manage arXiv paper ingestion and tracking',
};

export default function ArxivPage() {
  return <ArxivManagement />;
}