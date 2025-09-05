--
-- PostgreSQL database dump
--

\restrict jH0eOk3p97b9HwM8nA9loHOikZawPZN3Hqr5P7gEAJdFun7p44E7ovSYyNqCuiH

-- Dumped from database version 15.14 (Debian 15.14-1.pgdg12+1)
-- Dumped by pg_dump version 15.14 (Debian 15.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunks (
    id uuid NOT NULL,
    document_id uuid,
    chunk_index integer NOT NULL,
    text text NOT NULL,
    embedding public.vector(768) NOT NULL,
    char_count integer
);


--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id uuid NOT NULL,
    doc_id text NOT NULL,
    title text,
    dept text NOT NULL,
    lang text NOT NULL,
    path text NOT NULL,
    pages integer,
    characters integer,
    chunks integer,
    created_at timestamp without time zone DEFAULT now() NOT NULL,
    doc_code text,
    issued_by text,
    document_type text,
    issued_date date,
    valid_from date,
    valid_to date,
    tags jsonb,
    metadata jsonb,
    file_size bigint,
    sha1 text,
    ocr boolean DEFAULT false,
    sem_summary text,
    llm_summary text
);


--
-- Name: ocr_pages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ocr_pages (
    document_id text NOT NULL,
    page_no integer NOT NULL,
    text text
);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);


--
-- Name: documents documents_doc_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_doc_id_key UNIQUE (doc_id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: ocr_pages ocr_pages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ocr_pages
    ADD CONSTRAINT ocr_pages_pkey PRIMARY KEY (document_id, page_no);


--
-- Name: chunks_doc_chunk_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX chunks_doc_chunk_unique ON public.chunks USING btree (document_id, chunk_index);


--
-- Name: chunks_text_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_text_trgm ON public.chunks USING gin (text public.gin_trgm_ops);


--
-- Name: documents_dept_lang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX documents_dept_lang ON public.documents USING btree (dept, lang);


--
-- Name: documents_doc_code_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX documents_doc_code_key ON public.documents USING btree (doc_code);


--
-- Name: documents_path_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX documents_path_idx ON public.documents USING btree (path);


--
-- Name: documents_sha1_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX documents_sha1_idx ON public.documents USING btree (sha1);


--
-- Name: idx_chunks_doc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_doc ON public.chunks USING btree (document_id);


--
-- Name: idx_chunks_doc_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_doc_idx ON public.chunks USING btree (document_id, chunk_index);


--
-- Name: idx_chunks_docid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_docid ON public.chunks USING btree (document_id);


--
-- Name: idx_chunks_embed_cos; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_embed_cos ON public.chunks USING ivfflat (embedding public.vector_cosine_ops) WITH (lists='100');


--
-- Name: idx_chunks_text_gist; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_text_gist ON public.chunks USING gist (text public.gist_trgm_ops);


--
-- Name: idx_chunks_text_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chunks_text_trgm ON public.chunks USING gin (text public.gin_trgm_ops);


--
-- Name: idx_documents_dept_lang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_dept_lang ON public.documents USING btree (dept, lang);


--
-- Name: idx_documents_doc_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_doc_type ON public.documents USING btree (document_type);


--
-- Name: idx_documents_issued_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_issued_date ON public.documents USING btree (issued_date);


--
-- Name: idx_documents_metadata_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_metadata_gin ON public.documents USING gin (metadata);


--
-- Name: idx_documents_tags_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_tags_gin ON public.documents USING gin (tags);


--
-- Name: idx_documents_title_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_documents_title_trgm ON public.documents USING gin (title public.gin_trgm_ops);


--
-- Name: ocr_pages_doc_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ocr_pages_doc_idx ON public.ocr_pages USING btree (document_id);


--
-- Name: chunks chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict jH0eOk3p97b9HwM8nA9loHOikZawPZN3Hqr5P7gEAJdFun7p44E7ovSYyNqCuiH

