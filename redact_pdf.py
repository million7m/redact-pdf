import fitz
import sys
from pprint import pprint
from datetime import datetime
import re
from flask import Flask, request, send_file
import os
import tempfile

app = Flask(__name__)

def extract_text_from_pdf(pdf_path):
    """Extract address table and text blocks from Page 1 of a PDF file using PyMuPDF"""
    doc = fitz.open(pdf_path)
    text = ""
    pdf_data = {}
    
    for page_num, page in enumerate(doc, start=1):
        if page_num == 1:
            print("\nProcessing Page 1")
            
            blocks = page.get_text("blocks", sort=True)
            print("Blocks:", blocks)
            label = None
            for i, block in enumerate(blocks):
                if block[3] < 200 and block[4] != 'WORK ORDER\n':
                    block_text = block[4].replace('\n', '')
                    if ':' in block_text and not block_text.endswith(':'):
                        key_value = block_text.split(':', 1)
                        if len(key_value) == 2:
                            key, value = key_value
                            key = key.strip()
                            value = value.strip()
                            if key and value:
                                pdf_data[key] = value
                                print(f"Extracted key-value: {key} = {value}")
                                label = None
                                continue
                    if block[4].endswith(':\n'):
                        key = block[4].replace(':\n', '').strip()
                        if label is not None:
                            pdf_data[label] = ''
                            print(f"Assigned empty value: {label} = ''")
                        label = key
                        print(f"Key set: {label}")
                    elif label is not None:
                        pdf_data[label] = block[4].replace('\n', '')
                        label = None
            if label is not None:
                pdf_data[label] = ''
                print(f"Assigned empty value for last key: {label} = ''")
            
            tabs = page.find_tables()
            print(f"{len(tabs.tables)} table(s) found on Page 1")
            address_table = {}
            if tabs.tables:
                addr_table = tabs.tables[0].extract()
                addr_header = addr_table[0]
                addr_values = addr_table[1]
                for cnt, val in enumerate(addr_header):
                    address_table[val] = addr_values[cnt]
                print("table: address_table")
                print("Header:", addr_header)
                print("Values:", addr_values)
                print("Address table:", address_table)
            pdf_data['address_data'] = address_table
            
            wo_table = []
            if len(tabs.tables) > 1:
                wo_data = tabs.tables[1].extract()
                wo_header = wo_data[0]
                for row in wo_data[1:]:
                    wo_table.append({
                        'Code': row[wo_header.index('Code')],
                        'Description': row[wo_header.index('Description')],
                        'Quantity': row[wo_header.index('Quantity')],
                        'UOM': row[wo_header.index('UOM')],
                        'Rate': row[wo_header.index('Rate')],
                        'Amount': row[wo_header.index('Amount')]
                    })
                print("Work order table:", wo_table)
            pdf_data['wo_data'] = wo_table
            
            print("Final pdf_data:", pdf_data)
    
    return text, pdf_data, doc

def redact_pdf(doc, pdf_data, fields_to_redact):
    """Redact specified fields and work order table Rate/Amount in the PDF"""
    page = doc[0]
    for field in fields_to_redact:
        if field in pdf_data:
            value = pdf_data[field]
            text_instances = page.search_for(value)
            for inst in text_instances:
                page.add_redact_annot(inst, fill=(0, 0, 0))
                print(f"Redacted: {field} = {value}")
            key_value = f"{field}: {value}"
            text_instances = page.search_for(key_value)
            for inst in text_instances:
                page.add_redact_annot(inst, fill=(0, 0, 0))
                print(f"Redacted: {key_value}")
        
        if field in pdf_data['address_data']:
            value = pdf_data['address_data'][field]
            text_instances = page.search_for(value)
            for inst in text_instances:
                page.add_redact_annot(inst, fill=(0, 0, 0))
                print(f"Redacted: {field} = {value}")
    
    for item in pdf_data.get('wo_data', []):
        for field in ['Rate', 'Amount']:
            value = item[field]
            if value:
                text_instances = page.search_for(value)
                for inst in text_instances:
                    page.add_redact_annot(inst, fill=(0, 0, 0))
                    print(f"Redacted: {field} = {value}")
    
    page.apply_redactions()
    return doc

def transform_pdf_data(pdf_data):
    """Transform pdf_data into header and units dictionaries"""
    def parse_address(job_address):
        lines = job_address.split('\n')
        if len(lines) < 2:
            return {'street': job_address, 'city': '', 'state': '', 'zip': ''}
        last_line = lines[-1]
        parts = last_line.split(', ')
        if len(parts) != 2:
            return {'street': lines[0], 'city': '', 'state': '', 'zip': ''}
        city = parts[0]
        state_zip = parts[1].split(' ')
        state = state_zip[0] if len(state_zip) > 0 else ''
        zip_code = state_zip[1] if len(state_zip) > 1 else ''
        street = ' '.join(lines[:-1])
        return {'street': street, 'city': city, 'state': state, 'zip': zip_code}

    address_info = parse_address(pdf_data['address_data']['Job Address'])
    header = {
        'Prism_N': pdf_data.get('PRISM ID', ''),
        'PO_NetBuild': pdf_data.get('PO #', ''),
        'Date_Received': datetime.now().strftime('%Y-%m-%d'),
        'Coordinator': pdf_data.get('Const Coordinator', ''),
        'Customer': '#N/A',
        'STS_Rep': None,
        'Work_Type': pdf_data.get('Const Type', ''),
        'Address': address_info['street'],
        'City': address_info['city'],
        'State': address_info['state'],
        'Zip': address_info['zip'],
        'Project_Name': pdf_data['address_data'].get('Job', ''),
        'Estimated_Amount': pdf_data.get('PO Amount', ''),
        'ECD_Date': pdf_data.get('Vendor Name', '')
    }

    units = {
        'Prism_N': pdf_data.get('PRISM ID', ''),
        'Project_Name': pdf_data['address_data'].get('Job', '')
    }
    code_quantities = {}
    for item in pdf_data.get('wo_data', []):
        code = item['Code']
        try:
            quantity = int(float(item['Quantity']))
            code_quantities[code] = code_quantities.get(code, 0) + quantity
        except (ValueError, TypeError):
            print(f"Warning: Invalid quantity '{item['Quantity']}' for code '{code}'")
    
    units.update(code_quantities)
    return header, units

@app.route('/redact_pdf', methods=['POST'])
def redact_pdf_endpoint():
    """Flask endpoint to redact PDF and return the redacted file"""
    if 'file' not in request.files:
        return {"error": "No file provided"}, 400
    
    file = request.files['file']
    if not file.filename.endswith('.pdf'):
        return {"error": "File must be a PDF"}, 400
    
    # Save uploaded file temporarily
    input_fd, input_path = tempfile.mkstemp(suffix='.pdf')
    output_fd, output_path = tempfile.mkstemp(suffix='.pdf')
    try:
        with os.fdopen(input_fd, 'wb') as f:
            file.save(f)
        
        # Process the PDF
        text, pdf_data, doc = extract_text_from_pdf(input_path)
        doc = redact_pdf(doc, pdf_data, fields_to_redact=['PO Amount'])
        header, units = transform_pdf_data(pdf_data)
        
        # Save redacted PDF
        doc.save(output_path)
        doc.close()
        
        # Return the redacted PDF
        return send_file(output_path, as_attachment=True, download_name='redacted_output.pdf')
    
    finally:
        # Clean up temporary files
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):
            os.remove(output_path)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)